"""Bound supplementary odds capture without delaying the parent collector.

The persistent spawn child owns its HTTP client and writes only a temporary
response file. The parent remains the sole writer of research/collector logs.
"""
from datetime import datetime, timezone
import json
import math
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import uuid


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_NOTICE_BYTES = 4096


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('A timezone-aware deadline/completion time is required')
    return value.astimezone(timezone.utc)


def _notice(connection, value):
    connection.send_bytes(json.dumps(value, allow_nan=False).encode('utf-8'))


def _capture_child(connection):
    """Create one private client; never borrow the collector's session or clock."""
    from netkeirin import NetkeirinClient
    client = NetkeirinClient(timeout=3., retries=1)
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            try:
                odds = client.odds(request['race_id'])
                # Timestamp HTTP completion, before file serialization/IPC.
                completed = datetime.now(timezone.utc).isoformat(timespec='microseconds')
                payload = dict(request_id=request['request_id'], race_id=request['race_id'],
                               completed_at=completed, odds=odds)
                path = Path(request['output_path'])
                with path.open('w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                _notice(connection, dict(request_id=request['request_id'], status='ready'))
            except Exception as error:
                _notice(connection, dict(request_id=request['request_id'], status='error',
                                         error=type(error).__name__ + ': ' + str(error)[:200]))
    except (EOFError, OSError):
        pass
    finally:
        client.session.close()
        connection.close()


class TimedOddsCapture:
    """Capture only supplementary odds, with a maximum five-second wall budget.

    ``request`` includes spawn, file reads and JSON parsing in its measured
    budget. A timeout kills the child before its temporary directory is removed;
    a subsequent request can start a new child. Cleanup itself is bounded.
    """
    def __init__(self, budget_seconds=5., target=_capture_child):
        self.budget = self._budget(budget_seconds)
        self.target = target
        self.process = None
        self.connection = None

    @staticmethod
    def _budget(value):
        if isinstance(value, bool):
            raise ValueError('Capture budget must be in (0,5] seconds')
        value = float(value)
        if not math.isfinite(value) or not 0 < value <= 5.:
            raise ValueError('Capture budget must be in (0,5] seconds')
        return value

    def _start(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=self.target, args=(child,), daemon=True)
        self.connection, self.process = parent, process
        try:
            process.start()
        except BaseException:
            parent.close()
            self.connection = self.process = None
            raise
        finally:
            child.close()

    def request(self, race_id, *, budget_seconds, deadline):
        deadline = _utc(deadline)
        per_request = self._budget(budget_seconds)
        if not isinstance(race_id, str) or len(race_id) != 12 or not race_id.isdigit():
            raise ValueError('A twelve-digit race ID is required')
        started = time.monotonic()
        wall_started = datetime.now(timezone.utc)
        allowed = min(self.budget, per_request, (deadline - wall_started).total_seconds())
        if allowed <= 0:
            raise TimeoutError('Supplementary capture deadline already reached')

        def check_budget():
            remaining = allowed - (time.monotonic() - started)
            if remaining <= 0 or datetime.now(timezone.utc) >= deadline:
                raise TimeoutError('Supplementary capture exceeded wall-clock/deadline budget')
            return remaining

        request_id = uuid.uuid4().hex
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix='keirin-precutoff-', dir=os.environ.get('KEIRIN_TEST_TMP'))
            tmp = temporary.name
            output = Path(tmp) / 'response.json'
            if self.process is None:
                self._start()
            check_budget()
            # A small request cannot fill a pipe while the child is starting.
            self.connection.send(dict(race_id=race_id, request_id=request_id, output_path=str(output)))
            if not self.connection.poll(check_budget()):
                raise TimeoutError('Supplementary capture exceeded wall-clock/deadline budget')
            # Never receive an unbounded pickled HTTP response over the pipe.
            notice = json.loads(self.connection.recv_bytes(MAX_NOTICE_BYTES).decode('utf-8'))
            check_budget()
            if not isinstance(notice, dict) or notice.get('request_id') != request_id:
                raise ValueError('Supplementary capture response belongs to another request')
            if notice.get('status') == 'error':
                raise RuntimeError('Supplementary capture failed: ' + str(notice.get('error', 'unknown'))[:250])
            if notice.get('status') != 'ready':
                raise ValueError('Unknown supplementary capture response')
            if not 0 < output.stat().st_size <= MAX_RESPONSE_BYTES:
                raise ValueError('Supplementary capture response exceeds size limit')
            raw = output.read_bytes()
            check_budget()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('Supplementary capture response exceeds size limit')
            value = json.loads(raw)
            check_budget()
            if (not isinstance(value, dict) or set(value) != {'request_id', 'race_id', 'completed_at', 'odds'}
                    or value['request_id'] != request_id or value['race_id'] != race_id
                    or not isinstance(value['odds'], dict)):
                raise ValueError('Unbound or invalid supplementary capture data')
            completed = _utc(value['completed_at'])
            if not wall_started <= completed <= datetime.now(timezone.utc) or completed >= deadline:
                raise ValueError('Supplementary capture completion time is late or invalid')
            check_budget()
            return value['odds'], completed.isoformat(timespec='microseconds')
        except BaseException:
            self.close()
            raise
        finally:
            if temporary is not None:
                temporary.cleanup()

    def close(self):
        connection, process = self.connection, self.process
        self.connection = self.process = None
        if connection is not None:
            connection.close()
        if process is not None:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=.05)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=.1)
                if not process.is_alive():
                    process.close()
            except (OSError, ValueError, AssertionError):
                pass
