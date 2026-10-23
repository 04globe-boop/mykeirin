"""Collector-side evidence transport; frozen research code is never modified.

Release writers must share the workflow's short publication lock. GitHub Release
assets have no compare-and-swap operation, so semantic merging alone is not a lock.
"""
from collections import Counter
from datetime import datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo


class StateIntegrityError(RuntimeError):
    pass


def days_for_window(now, window_minutes):
    """All dates the logger can watch, including its before-03:00 previous day."""
    if window_minutes < 0:
        raise ValueError('Negative execution window')
    jst = ZoneInfo('Asia/Tokyo')
    now = now.replace(tzinfo=jst) if now.tzinfo is None else now.astimezone(jst)
    first = now.date() - timedelta(days=int(now.hour < 3))
    last = (now + timedelta(minutes=window_minutes)).date()
    return [(first + timedelta(days=n)).strftime('%Y%m%d')
            for n in range((last-first).days+1)]


def asset_day(name):
    match = re.search(r'_(\d{8})\.(?:sqlite|jsonl|json)(?:\.gz)?$', name)
    return match[1] if match else None


def asset_kind(name):
    if name.endswith('.sqlite.gz'):
        return 'immutable_ledger'
    if name == 'research_history.jsonl.gz':
        return 'immutable_history'
    if name.endswith('.jsonl.gz'):
        return 'append_collection'
    if name.endswith('.json'):
        return 'mutable_report'
    raise StateIntegrityError('Unsupported research asset: '+name)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compress(source, destination):
    with Path(source).open('rb') as src, Path(destination).open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as dest:
            shutil.copyfileobj(src, dest)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _json_rows(path):
    if path is None:
        return []
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
        result = []
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise StateIntegrityError('Research JSONL requires object rows')
            result.append((value, line if line.endswith('\n') else line+'\n'))
        return result


def _merge_rows(local, remote, destination, *, history=False):
    remote_rows, local_rows = _json_rows(remote), _json_rows(local)
    # Events may repeat; retain max multiplicity rather than erasing attempts or
    # duplicating the restored prefix on every publication. Inputs can legitimately
    # contain different snapshots for the same race and are keyed by the full row.
    seen = Counter(_canonical(value) for value, _ in remote_rows)
    if history:
        keyed = {}
        for value, _ in remote_rows+local_rows:
            key = value.get('race_id')
            if not key:
                raise StateIntegrityError('History race_id required')
            normalized = _canonical(value)
            if key in keyed and keyed[key] != normalized:
                raise StateIntegrityError('Conflicting immutable history race: '+str(key))
            keyed[key] = normalized
    result = list(remote_rows)
    counts = Counter()
    for value, raw in local_rows:
        key = _canonical(value)
        counts[key] += 1
        if counts[key] > seen[key]:
            result.append((value, raw))
    with gzip.open(destination, 'wt', encoding='utf-8', newline='\n') as stream:
        for _, raw in result:
            stream.write(raw)


def _read_ledger(path):
    db = sqlite3.connect('file:'+Path(path).resolve().as_posix()+'?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise StateIntegrityError('Invalid research ledger')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not tables.issubset({'predictions', 'labels'}) or 'predictions' not in tables:
            raise StateIntegrityError('Unexpected research ledger tables')
        result = {}
        for table in sorted(tables):
            columns = db.execute('PRAGMA table_info('+table+')').fetchall()
            keys = [i for i, column in sorted(enumerate(columns), key=lambda pair: pair[1][5]) if column[5]]
            if not keys:
                raise StateIntegrityError('Immutable ledger key required')
            rows = {}
            for row in db.execute('SELECT * FROM '+table):
                normalized = tuple(_canonical(json.loads(value)) if columns[i][1] in ('payload','truth')
                                   and value is not None else value for i, value in enumerate(row))
                key = tuple(row[i] for i in keys)
                if key in rows:
                    raise StateIntegrityError('Duplicate immutable ledger key')
                rows[key] = (normalized, row)
            result[table] = (columns, rows)
        return result
    finally:
        db.close()


def _merge_ledgers(local, remote, destination):
    with tempfile.TemporaryDirectory(prefix='ledger-merge-', dir=Path(destination).parent) as tmp:
        root = Path(tmp)
        unpacked = []
        for name, source in (('local', local), ('remote', remote)):
            if source is not None:
                path = root / (name+'.sqlite')
                with gzip.open(source, 'rb') as src, path.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                unpacked.append(path)
        inventories = [_read_ledger(path) for path in unpacked]
        original = inventories[0]
        for other in inventories[1:]:
            if set(original) != set(other):
                raise StateIntegrityError('Research ledger schema differs')
            for table, (columns, table_rows) in original.items():
                if columns != other[table][0]:
                    raise StateIntegrityError('Research ledger columns differ')
                for key, (normalized, _) in other[table][1].items():
                    if key in table_rows and table_rows[key][0] != normalized:
                        raise StateIntegrityError('Conflicting immutable '+table+' key: '+str(key))
        merged = root/'merged.sqlite'
        shutil.copy2(unpacked[0], merged)
        db = sqlite3.connect(merged)
        try:
            for other in inventories[1:]:
                for table, (columns, table_rows) in other.items():
                    for key, (_, row) in table_rows.items():
                        if key not in original[table][1]:
                            db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in columns)+')', row)
            db.commit()
        finally:
            db.close()
        compress(merged, destination)


def merge_asset(local, remote, destination, name=None):
    """Write a semantic union or reject conflicting immutable keys before publish."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    kind = asset_kind(name or destination.name)
    if kind == 'immutable_ledger':
        _merge_ledgers(local, remote, destination)
    elif kind in ('append_collection', 'immutable_history'):
        _merge_rows(local, remote, destination, history=kind == 'immutable_history')
    else:
        source = local if local is not None else remote
        json.loads(Path(source).read_text(encoding='utf-8'))
        shutil.copy2(source, destination)
    return destination


def issuance_by_race(ledger_path, candidate_ids):
    """Cohort-specific issuance; partial candidate sets remain visible to audits."""
    path = Path(ledger_path)
    if not path.exists():
        return {}
    db = sqlite3.connect('file:'+path.resolve().as_posix()+'?mode=ro', uri=True)
    try:
        result = {}
        candidates = set(candidate_ids)
        for race, model in db.execute('SELECT race_id,model_id FROM predictions'):
            if model in candidates:
                result.setdefault(race, set()).add(model)
        return result
    finally:
        db.close()
