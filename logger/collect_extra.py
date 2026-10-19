"""Kドリームスとオッズパークのページから、走り評・記者評価・選手コメント・レース後談話を集める。

1日ずつ netkeirin のレース一覧（AplRace）を取り、各レースについて
  - Kドリームスのレース詳細（予想印・好気合・総評・選手コメント・前回の走り評／短評・結果の勝敗因・狙い目・戦い終わって）
  - オッズパークの出走表（選手コメント。Kドリームスとは別の取材文）
を取得し、必要な部分だけを保存する（page_parse.py）。保存済みのレースは飛ばすので、途中で止まっても再開できる。

保存形式：<out-dir>/extra_YYYYMM.jsonl.gz（1行1レコード、gzip の追記）
  type=k      Kドリームスの1レース分
  type=op     オッズパークの1レース分
  type=error  取得や解析の失敗（推測で埋めない。HTTPの状態コードを残す）
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netkeirin import NetkeirinClient  # noqa: E402
from page_parse import parse_kdreams, parse_oddspark  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
VENUES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kdreams_venues.json"), encoding="utf-8"))


class Fetcher:
    """ドメインごとに間隔を空けて取得する。失敗は3回まで再試行。"""

    def __init__(self, interval: float):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ja"})
        self.interval = interval
        self.last: dict[str, float] = {}

    def get(self, url: str):
        host = url.split("/")[2]
        err = None
        for attempt in range(3):
            wait = self.interval - (time.monotonic() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code >= 500:
                    err = f"HTTP {r.status_code}"
                    time.sleep(3 * (attempt + 1))
                    continue
                return r.status_code, r.content
            except requests.RequestException as e:
                err = str(e)[:200]
                time.sleep(3 * (attempt + 1))
        return None, err


def kdreams_url(race: dict) -> str | None:
    rid = race["race_id"]
    slug = VENUES.get(rid[8:10])
    first = race.get("kaisai_date_1st") or ""
    try:
        nichiji = int(race.get("nichiji") or 0)
    except ValueError:
        nichiji = 0
    if not slug or len(first) != 8 or not nichiji:
        return None
    return f"https://keirin.kdreams.jp/{slug}/racedetail/{rid[8:10]}{first}{nichiji:02d}00{rid[10:12]}/"


def oddspark_url(race: dict) -> str:
    rid = race["race_id"]
    return f"https://sp.oddspark.com/keirin/SpRaceInfo.do?joCd={rid[8:10]}&kaisaiBi={rid[:8]}&raceNo={int(rid[10:12])}"


def done_keys(path: str) -> set:
    keys = set()
    if not os.path.exists(path):
        return keys
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") in ("k", "op", "k_pre", "op_pre"):
                keys.add((r["type"].replace("_pre", ""), r["race_id"]))
    return keys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="YYYY-MM（その月の昨日までを収集）")
    g.add_argument("--date", help="YYYY-MM-DD（1日分）")
    g.add_argument("--pre", action="store_true",
                   help="締切前の取得：今日のレースのうち、締切が --lead-min〜--window-min 分後のものを取る（レース前の時点の記録）")
    ap.add_argument("--lead-min", type=float, default=10)
    ap.add_argument("--window-min", type=float, default=100)
    ap.add_argument("--out-dir", default="extra")
    ap.add_argument("--max-minutes", type=float, default=320)
    ap.add_argument("--interval", type=float, default=1.0, help="同じサイトへのリクエスト間隔（秒）")
    ap.add_argument("--status-file")
    a = ap.parse_args(argv)

    now0 = datetime.now(JST)
    if a.pre:
        days = [now0.date()]
    elif a.month:
        y, m = (int(x) for x in a.month.split("-"))
        days, d = [], date(y, m, 1)
        while d.month == m:
            days.append(d)
            d += timedelta(days=1)
    else:
        days = [date.fromisoformat(a.date)]
    ym = days[0].strftime("%Y%m")
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"extra_pre_{days[0].strftime('%Y%m%d')}.jsonl.gz" if a.pre else f"extra_{ym}.jsonl.gz")
    have = done_keys(path)
    nk = NetkeirinClient(min_interval=0.8)
    fetchers = {"k": Fetcher(a.interval), "op": Fetcher(a.interval)}  # サイトごとに別々に取得（2つを同時に進める）
    pool = ThreadPoolExecutor(max_workers=2)
    t0 = time.monotonic()
    today = datetime.now(JST).date()
    n_new = n_err = 0

    def write(rec):
        with gzip.open(path, "at", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    def status(state, **kw):
        if a.status_file:
            with open(a.status_file, "w", encoding="utf-8") as f:
                json.dump({"state": state, **kw}, f, ensure_ascii=False)

    for d in days:
        if d >= today and not a.pre:
            break
        ds = d.strftime("%Y%m%d")
        try:
            races = nk.race_list(ds)
        except Exception as e:  # noqa: BLE001
            write({"type": "error", "what": "racelist", "kaisai_date": ds, "error": str(e)[:300]})
            n_err += 1
            continue
        for race in races:
            rid = race.get("race_id")
            if not rid:
                continue
            if a.pre:
                try:
                    hh, mm = (int(x) for x in str(race.get("close") or "").split(":"))
                except ValueError:
                    continue
                close = datetime.combine(d, datetime.min.time(), tzinfo=JST) + timedelta(hours=hh, minutes=mm)
                lead = (close - datetime.now(JST)).total_seconds() / 60
                if not (a.lead_min <= lead <= a.window_min):
                    continue
            if (time.monotonic() - t0) / 60 > a.max_minutes:
                status("time_limit", new=n_new, errors=n_err, at=ds)
                print(json.dumps({"stopped": "time limit", "new": n_new, "errors": n_err, "at": ds}))
                return 0
            todo = [(kind, url, parser) for kind, url, parser in
                    (("k", kdreams_url(race), parse_kdreams), ("op", oddspark_url(race), parse_oddspark))
                    if (kind, rid) not in have]
            futs = {kind: pool.submit(fetchers[kind].get, url) for kind, url, _ in todo if url}
            for kind, url, parser in todo:
                now = datetime.now(JST).isoformat(timespec="seconds")
                if url is None:
                    write({"type": "error", "what": kind, "race_id": rid, "error": "no url (venue or kaisai info missing)"})
                    n_err += 1
                    continue
                code, body = futs[kind].result()
                if code != 200:
                    write({"type": "error", "what": kind, "race_id": rid, "url": url, "status": code,
                           "error": body if isinstance(body, str) else None, "fetched_at": now})
                    n_err += 1
                    continue
                try:
                    data = parser(body)
                except Exception as e:  # noqa: BLE001
                    write({"type": "error", "what": kind, "race_id": rid, "url": url, "error": "parse: " + str(e)[:200]})
                    n_err += 1
                    continue
                write({"type": kind + ("_pre" if a.pre else ""), "race_id": rid, "url": url, "fetched_at": now,
                       **({"close": race.get("close")} if a.pre else {}), "data": data})
                have.add((kind, rid))
                n_new += 1
        print(f"{ds}: races={len(races)} total_new={n_new} errors={n_err}", flush=True)
    status("finished", new=n_new, errors=n_err)
    print(json.dumps({"finished": a.month or a.date or "pre", "new": n_new, "errors": n_err}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
