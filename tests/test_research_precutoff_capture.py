import json
import os
from pathlib import Path
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
from precutoff_capture import TimedOddsCapture, MAX_RESPONSE_BYTES, _notice


def write_result(connection, request, *, odds=None, completed=None):
    completed = completed or datetime.now(timezone.utc).isoformat(timespec='microseconds')
    payload = dict(request_id=request['request_id'], race_id=request['race_id'],
                   completed_at=completed, odds=odds if odds is not None else {'list_9': [['010203', 5.6]]})
    Path(request['output_path']).write_text(json.dumps(payload), encoding='utf-8')
    _notice(connection, dict(request_id=request['request_id'], status='ready'))


def echo_child(connection):
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            write_result(connection, request)
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


def slow_child(connection):
    connection.recv()
    time.sleep(30)


def warm_then_hang(connection):
    write_result(connection, connection.recv())
    connection.recv()
    time.sleep(30)


def late_child(connection):
    request = connection.recv()
    write_result(connection, request, completed=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    connection.close()


def oversized_child(connection):
    request = connection.recv()
    Path(request['output_path']).write_bytes(b'x' * (MAX_RESPONSE_BYTES + 1))
    _notice(connection, dict(request_id=request['request_id'], status='ready'))
    connection.close()


def oversized_notice_child(connection):
    connection.recv()
    connection.send_bytes(b'x' * 8192)
    connection.close()


class PrecutoffCaptureBudget(unittest.TestCase):
    def deadline(self, seconds=10):
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def test_success_preserves_http_completion_and_reuses_own_process(self):
        capture = TimedOddsCapture(target=echo_child)
        before = datetime.now(timezone.utc)
        try:
            odds, completed = capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
            self.assertEqual(odds, {'list_9': [['010203', 5.6]]})
            self.assertLessEqual(before, datetime.fromisoformat(completed))
            self.assertLessEqual(datetime.fromisoformat(completed), datetime.now(timezone.utc))
            process = capture.process
            capture.request('202610202702', budget_seconds=5., deadline=self.deadline())
            self.assertIs(capture.process, process)
        finally:
            capture.close()
        self.assertIsNone(capture.process)
        self.assertIsNone(capture.connection)
        capture.close()

    def test_actual_hung_child_is_stopped_and_capture_can_restart(self):
        capture = TimedOddsCapture(target=warm_then_hang)
        # First prove the spawned child is running; the timeout below measures
        # an actual unresponsive request rather than only spawn/startup time.
        capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            capture.request('202610202701', budget_seconds=.25, deadline=self.deadline())
        self.assertLess(time.monotonic() - started, 2.)
        self.assertIsNone(capture.process)
        capture.target = echo_child
        try:
            odds, _ = capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
            self.assertIn('list_9', odds)
        finally:
            capture.close()

    def test_deadline_shorter_than_budget_also_stops_unresponsive_child(self):
        capture = TimedOddsCapture(target=slow_child)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            capture.request('202610202701', budget_seconds=5., deadline=self.deadline(.25))
        self.assertLess(time.monotonic() - started, 2.)
        self.assertIsNone(capture.process)

    def test_invalid_budget_and_expired_deadline_never_start_process(self):
        for bad in (0., -1., 5.1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                TimedOddsCapture(budget_seconds=bad)
        capture = TimedOddsCapture(target=slow_child)
        with self.assertRaises(ValueError):
            capture.request('202610202701', budget_seconds=5., deadline=datetime.now())
        with self.assertRaises(TimeoutError):
            capture.request('202610202701', budget_seconds=5., deadline=self.deadline(-1))
        with self.assertRaises(ValueError):
            capture.request('202610202701', budget_seconds=6., deadline=self.deadline())
        self.assertIsNone(capture.process)

    def test_future_completion_timestamp_is_rejected(self):
        capture = TimedOddsCapture(target=late_child)
        try:
            with self.assertRaisesRegex(ValueError, 'completion time'):
                capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
            self.assertIsNone(capture.process)
        finally:
            capture.close()

    def test_oversized_file_is_rejected_without_parsing_large_response(self):
        capture = TimedOddsCapture(target=oversized_child)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ValueError, 'size limit'):
                capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
            self.assertLess(time.monotonic() - started, 5.5)
            self.assertIsNone(capture.process)
        finally:
            capture.close()

    def test_oversized_pipe_notice_is_bounded(self):
        capture = TimedOddsCapture(target=oversized_notice_child)
        try:
            with self.assertRaises(OSError):
                capture.request('202610202701', budget_seconds=5., deadline=self.deadline())
            self.assertIsNone(capture.process)
        finally:
            capture.close()


if __name__ == '__main__':
    unittest.main()
