import gzip
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "logger"))
from run_logger import JST, Logger  # noqa: E402
from netkeirin import parse_lines  # noqa: E402


class FakeClock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class FakeClient:
    def __init__(self, clock, races):
        self.clock, self.races, self.calls = clock, races, []

    def _tick(self):
        self.clock.t += timedelta(seconds=1)

    def race_list(self, d):
        self._tick()
        return [r for r in self.races if r["kaisai_date"] == d]

    def odds(self, rid):
        self._tick()
        self.calls.append(("odds", rid, self.clock.t))
        return {"official_dt": "", "list_6": [["0102", "5.6", "", "1"]]}

    def lines(self, rid):
        self._tick()
        return {"lineForecast": [[{"syaban": "5"}, {"syaban": "3"}, {"syaban": "0"}, {"syaban": "8"},
                                  {"syaban": "7"}, {"syaban": "1"}, {"syaban": "0"}, {"syaban": "0"},
                                  {"syaban": "6"}]]}

    def entries(self, rid):
        self._tick()
        return [{"syaban": "1"}]


def mk(rid, close):
    return {"race_id": rid, "kaisai_date": close.strftime("%Y%m%d"), "close": close.strftime("%H:%M"),
            "start": (close + timedelta(minutes=5)).strftime("%H:%M"), "race_status": "", "sale_status": ""}


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f]


def test_parse_lines():
    data = {"lineForecast": [[{"syaban": "5"}, {"syaban": "3"}, {"syaban": "0"}, {"syaban": "8"}, {"syaban": "7"},
                              {"syaban": "1"}, {"syaban": "0"}, {"syaban": "0"}, {"syaban": "6"}, {"syaban": "2"},
                              {"syaban": "4"}]]}
    assert parse_lines(data) == [["5", "3"], ["8", "7", "1"], ["6", "2", "4"]]


def test_normal_window():
    t0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=JST)
    ck = FakeClock(t0)
    races = [mk("202610192701", t0 + timedelta(minutes=6)), mk("202610192702", t0 + timedelta(minutes=70))]
    cl = FakeClient(ck, races)
    with tempfile.TemporaryDirectory() as d:
        s = Logger(cl, d, 12, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        recs = read(os.path.join(d, "odds_20261019.jsonl.gz"))
        odds = [(r["race_id"][-2:], r["label"]) for r in recs if r["type"] == "odds"]
        # 1R: -5,-3,-2,-1 は窓内。-7 は起動1分前の予定で許容範囲内なので取り直し。2R: -60 が窓内(10分後)
        assert ("01", "-5") in odds and ("01", "-1") in odds and ("02", "-60") in odds, odds
        assert ("01", "-10") not in odds  # 10分前は起動4分前→許容2分を超えるので取り逃し記録
        misses = [(r["race_id"][-2:], r["label"]) for r in recs if r["type"] == "miss"]
        assert ("01", "-10") in misses, misses
        # 実際の取得時刻と予定時刻の差は小さい
        for r in recs:
            if r["type"] == "odds" and r["race_id"].endswith("01") and r["label"] in ("-5", "-3", "-2", "-1"):
                lag = (datetime.fromisoformat(r["fetched_at"]) - datetime.fromisoformat(r["target_at"])).total_seconds()
                assert 0 <= lag <= 5, (r["label"], lag)
        lines = [r for r in recs if r["type"] == "lines"]
        assert {r["label"] for r in lines} == {"-5", "-60"}
        assert lines[0]["parsed"][0] == ["5", "3"]
        assert s["error"] == 0


def test_resume_no_duplicates():
    t0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=JST)
    ck = FakeClock(t0)
    races = [mk("202610192701", t0 + timedelta(minutes=20))]
    cl = FakeClient(ck, races)
    with tempfile.TemporaryDirectory() as d:
        Logger(cl, d, 8, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        n1 = len(cl.calls)
        Logger(cl, d, 8, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        recs = read(os.path.join(d, "odds_20261019.jsonl.gz"))
        labels = [r["label"] for r in recs if r["type"] == "odds"]
        assert len(labels) == len(set(labels)), labels
        assert len(cl.calls) > n1
        # racelist は内容が同じなら1回だけ保存
        assert sum(1 for r in recs if r["type"] == "racelist") == 1


def test_post_close_final():
    t0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=JST)
    ck = FakeClock(t0)
    races = [mk("202610192701", t0 - timedelta(minutes=10))]  # 10分前に締切済み
    cl = FakeClient(ck, races)
    with tempfile.TemporaryDirectory() as d:
        Logger(cl, d, 5, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        recs = read(os.path.join(d, "odds_20261019.jsonl.gz"))
        assert any(r["type"] == "odds" and r["label"] == "+8" for r in recs)
        assert all(r["label"] != "-1" for r in recs if r["type"] == "odds")


def test_final_stops_retries():
    t0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=JST)
    ck = FakeClock(t0)
    races = [mk("202610192701", t0 - timedelta(minutes=9))]
    cl = FakeClient(ck, races)
    cl.odds = lambda rid: (cl._tick(), {"official_dt": "2026-10-19 14:57:00"})[1]
    with tempfile.TemporaryDirectory() as d:
        Logger(cl, d, 10, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        recs = read(os.path.join(d, "odds_20261019.jsonl.gz"))
        labels = [r["label"] for r in recs if r["type"] == "odds"]
        assert labels == ["+8"], labels  # 確定値が取れたので +15 は取らない


def test_old_misses_not_written():
    t0 = datetime(2026, 10, 19, 18, 0, 0, tzinfo=JST)
    ck = FakeClock(t0)
    races = [mk("202610192701", t0 - timedelta(hours=3))]
    cl = FakeClient(ck, races)
    with tempfile.TemporaryDirectory() as d:
        Logger(cl, d, 3, clock=ck.now, sleeper=ck.sleep, log=lambda *_: None).run()
        recs = read(os.path.join(d, "odds_20261019.jsonl.gz"))
        assert not [r for r in recs if r["type"] == "miss"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
