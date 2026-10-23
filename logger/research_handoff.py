"""Move captured evidence between jobs through a private, immutable Release.

Only data and audit manifests cross this boundary. Frozen executable sources,
weights, checkout files and credentials are never selected or extracted.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 2 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FILES = 1000
MANIFEST = '_research_handoff_manifest.json'
SCHEMA = 'research-capture-handoff-v1'
RECEIPT_SCHEMA = 'research-capture-receipt-v1'
HEX = re.compile(r'^[0-9a-f]{64}$')
FORWARD = r'forward_[A-Za-z0-9_-]+\.(?:sqlite\.gz|jsonl\.gz|json)'
RESERVED = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


class HandoffError(RuntimeError):
    pass


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json(body):
    if len(body) > MAX_JSON_BYTES:
        raise HandoffError('JSON metadata is oversized')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise HandoffError('Duplicate JSON metadata key')
            result[key] = value
        return result
    def invalid(value):
        raise HandoffError('Non-finite JSON metadata')
    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as error:
        raise HandoffError('Malformed JSON metadata') from error
    if not isinstance(value, dict):
        raise HandoffError('JSON metadata must be an object')
    return value


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def _gh(*args):
    try:
        result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HandoffError('Private handoff GitHub operation unavailable') from error
    if result.returncode:
        raise HandoffError('Private handoff GitHub operation failed')
    return result.stdout


def _private_repo(repo):
    value = _json(_gh('api', 'repos/'+repo).encode('utf-8'))
    if value.get('private') is not True or str(value.get('full_name', '')).lower() != repo.lower():
        raise HandoffError('Capture handoff requires the selected private repository')


def _identity():
    run = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[1-9][0-9]*', run) or not re.fullmatch(r'[1-9][0-9]*', attempt):
        raise HandoffError('GitHub run ID and attempt are required')
    return run, attempt, 'research-capture-'+run+'-'+attempt


def _safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and path.as_posix() == name
            and all(part not in ('', '.', '..') and not RESERVED.match(part)
                    and not part.endswith((' ', '.')) for part in path.parts))


def _allowed(name):
    if not _safe_name(name):
        return False
    if re.fullmatch(r'data/(?:'+FORWARD+r'|odds_[0-9]{8}\.jsonl\.gz|logger_summary_[0-9]{8}(?:_[A-Za-z0-9_-]+)?\.json)', name):
        return True
    if name in ('data/research_transport.json', 'data/research_pack.json',
                'research-runtime/active_bundles.json', 'research-runtime/preparation.json'):
        return True
    if re.fullmatch(r'data/research_restored/(?:[A-Za-z0-9_-]+/)*[0-9a-f]{64}-(?:'+FORWARD+r'|odds_[0-9]{8}\.jsonl\.gz|research_history\.jsonl\.gz)', name):
        return True
    return bool(re.fullmatch(r'research-runtime/(?:[A-Za-z0-9_-]+/)+bundle/bundle\.json', name))


def _source_path(root, path):
    if not path.resolve().is_relative_to(root):
        raise HandoffError('Source path escapes capture root')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise HandoffError('Capture symlinks are not transferable')
    if not path.is_file():
        raise HandoffError('Capture source is not a regular file')


def _sources(root):
    candidates = []
    data = root / 'data'
    if data.exists():
        candidates.extend(data.iterdir())
        restored = data / 'research_restored'
        if restored.is_dir():
            candidates.extend(restored.rglob('*'))
    runtime = root / 'research-runtime'
    candidates.extend((runtime/'active_bundles.json', runtime/'preparation.json'))
    if runtime.is_dir():
        candidates.extend(runtime.glob('**/bundle/bundle.json'))
    selected = {}
    for path in candidates:
        name = path.relative_to(root).as_posix()
        if _allowed(name) and path.exists():
            _source_path(root, path)
            selected[name] = path
    if 'data/research_pack.json' not in selected:
        raise HandoffError('Pack captured state before staging the handoff')
    if len(selected) > MAX_FILES:
        raise HandoffError('Too many capture files')
    return sorted(selected.items())


def stage(repo, root='.'):
    root = Path(root).resolve()
    run, attempt, tag = _identity()
    _private_repo(repo)
    sources = _sources(root)
    archive_name, receipt_name = tag+'.zip', tag+'.receipt.json'
    with tempfile.TemporaryDirectory(prefix='research-handoff-', dir=root) as tmp:
        archive_path = Path(tmp)/archive_name
        files = []
        total = 0
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, source in sources:
                size = source.stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise HandoffError('Capture data exceeds handoff capacity')
                if name.endswith('.json'):
                    _json(source.read_bytes())
                digest = hashlib.sha256()
                copied = 0
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with source.open('rb') as src, archive.open(info, 'w') as dest:
                    for chunk in iter(lambda: src.read(1024*1024), b''):
                        copied += len(chunk)
                        if copied > size:
                            raise HandoffError('Capture file changed while staging')
                        digest.update(chunk)
                        dest.write(chunk)
                if copied != size:
                    raise HandoffError('Capture file changed while staging')
                files.append({'path':name, 'size':size, 'sha256':digest.hexdigest()})
            manifest = {'schema':SCHEMA, 'run_id':run, 'run_attempt':attempt, 'files':files,
                        'created_at':datetime.now(timezone.utc).isoformat()}
            manifest_bytes = _encoded(manifest)
            if len(manifest_bytes) > MAX_JSON_BYTES or total+len(manifest_bytes) > MAX_TOTAL_BYTES:
                raise HandoffError('Capture manifest exceeds capacity')
            archive.writestr(MANIFEST, manifest_bytes)
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise HandoffError('Capture archive exceeds capacity')
        receipt = {'schema':RECEIPT_SCHEMA, 'run_id':run, 'run_attempt':attempt, 'tag':tag,
                   'archive':archive_name, 'receipt':receipt_name, 'size':archive_path.stat().st_size,
                   'sha256':_hash(archive_path), 'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),
                   'files':len(files)}
        receipt_path = Path(tmp)/receipt_name
        receipt_path.write_bytes(_encoded(receipt))
        # A repeated stage cannot replace an existing run or its assets.
        _gh('release', 'create', tag, '--repo', repo, '--title', 'Private research capture '+run,
            '--notes', 'Immutable capture handoff; private research evidence.', '--latest=false')
        _gh('release', 'upload', tag, str(archive_path), str(receipt_path), '--repo', repo)
        return receipt


def _same_identity(value, schema, run, attempt):
    if value.get('schema') != schema or value.get('run_id') != run or value.get('run_attempt') != attempt:
        raise HandoffError('Handoff schema or run identity differs')


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _target(root, name, digest):
    path = root/name
    current = root
    for part in PurePosixPath(name).parts:
        current = current/part
        if current.is_symlink():
            raise HandoffError('Existing destination symlink is forbidden')
        if current != path and current.exists() and not current.is_dir():
            raise HandoffError('Existing destination parent is not a directory')
    if not path.resolve().is_relative_to(root):
        raise HandoffError('Destination escapes checkout root')
    if path.exists() and (not path.is_file() or _hash(path) != digest):
        raise HandoffError('Existing destination content will not be overwritten')
    return path


def _verified_files(archive_path, receipt, destination, run, attempt):
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise HandoffError('Invalid capture archive') from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES+1:
            raise HandoffError('Too many archived files')
        names = set()
        total = 0
        by_name = {}
        for info in infos:
            name = info.filename
            mode = (info.external_attr >> 16) & 0xffff
            if (info.orig_filename != name or not _safe_name(name) or (name != MANIFEST and not _allowed(name)) or info.is_dir()
                    or name.casefold() in names or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or info.flag_bits & 1 or info.compress_type != zipfile.ZIP_STORED):
                raise HandoffError('Unsafe or unsupported archived member')
            names.add(name.casefold())
            total += info.file_size
            if info.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise HandoffError('Unpacked handoff exceeds capacity')
            by_name[name] = info
        if MANIFEST not in by_name or by_name[MANIFEST].file_size > MAX_JSON_BYTES:
            raise HandoffError('Capture manifest unavailable or oversized')
        manifest_bytes = archive.read(MANIFEST)
        if hashlib.sha256(manifest_bytes).hexdigest() != receipt.get('manifest_sha256'):
            raise HandoffError('Capture manifest hash differs')
        manifest = _json(manifest_bytes)
        _same_identity(manifest, SCHEMA, run, attempt)
        files = manifest.get('files')
        if not isinstance(files, list) or len(files) != receipt.get('files') or len(files) > MAX_FILES:
            raise HandoffError('Capture manifest file count differs')
        wanted = set()
        for item in files:
            if not isinstance(item, dict):
                raise HandoffError('Malformed capture file metadata')
            name, size, digest = item.get('path'), item.get('size'), item.get('sha256')
            if (not _allowed(name) or name in wanted or not _integer(size)
                    or not isinstance(digest, str) or not HEX.fullmatch(digest)
                    or name not in by_name or size != by_name[name].file_size):
                raise HandoffError('Capture file metadata differs')
            wanted.add(name)
        if set(by_name) != wanted | {MANIFEST}:
            raise HandoffError('Capture archive and manifest members differ')
        # Validate every payload into a disposable directory before touching data/
        # or research-runtime/ in the receiving checkout.
        for item in files:
            name = item['path']; path = destination/name
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(); size = 0
            try:
                with archive.open(name) as src, path.open('xb') as dest:
                    for chunk in iter(lambda: src.read(1024*1024), b''):
                        size += len(chunk)
                        if size > item['size']:
                            raise HandoffError('Archived payload exceeds declared size')
                        digest.update(chunk); dest.write(chunk)
            except (OSError, zipfile.BadZipFile, RuntimeError) as error:
                raise HandoffError('Capture payload cannot be verified') from error
            if size != item['size'] or digest.hexdigest() != item['sha256']:
                raise HandoffError('Capture payload hash differs')
            if name.endswith('.json'):
                _json(path.read_bytes())
        return files


def receive(repo, root='.'):
    root = Path(root).resolve()
    run, attempt, tag = _identity()
    _private_repo(repo)
    archive_name, receipt_name = tag+'.zip', tag+'.receipt.json'
    with tempfile.TemporaryDirectory(prefix='research-handoff-', dir=root) as tmp:
        staging = Path(tmp)
        _gh('release', 'download', tag, '--repo', repo, '-p', receipt_name, '-D', str(staging))
        receipt_path = staging/receipt_name
        if not receipt_path.is_file() or receipt_path.stat().st_size > MAX_JSON_BYTES:
            raise HandoffError('Handoff receipt unavailable or oversized')
        receipt = _json(receipt_path.read_bytes())
        _same_identity(receipt, RECEIPT_SCHEMA, run, attempt)
        if (receipt.get('archive') != archive_name or receipt.get('receipt') != receipt_name or receipt.get('tag') != tag
                or not _integer(receipt.get('size')) or receipt['size'] > MAX_ARCHIVE_BYTES
                or not isinstance(receipt.get('sha256'), str) or not HEX.fullmatch(receipt['sha256'])
                or not isinstance(receipt.get('manifest_sha256'), str) or not HEX.fullmatch(receipt['manifest_sha256'])
                or not _integer(receipt.get('files')) or receipt['files'] > MAX_FILES):
            raise HandoffError('Handoff receipt differs from expected capture')
        _gh('release', 'download', tag, '--repo', repo, '-p', archive_name, '-D', str(staging))
        archive_path = staging/archive_name
        if not archive_path.is_file() or archive_path.stat().st_size != receipt['size'] or _hash(archive_path) != receipt['sha256']:
            raise HandoffError('Handoff archive hash or size differs')
        verified = staging/'verified'
        files = _verified_files(archive_path, receipt, verified, run, attempt)
        for item in files:
            _target(root, item['path'], item['sha256'])
        for item in files:
            path = _target(root, item['path'], item['sha256'])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with (verified/item['path']).open('rb') as src, path.open('xb') as dest:
                    shutil.copyfileobj(src, dest)
        return {'schema':SCHEMA, 'tag':tag, 'archive':archive_name, 'sha256':receipt['sha256'], 'files':len(files)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['stage', 'receive'])
    parser.add_argument('--repo', required=True)
    parser.add_argument('--root', default='.')
    args = parser.parse_args()
    result = stage(args.repo, args.root) if args.command == 'stage' else receive(args.repo, args.root)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
