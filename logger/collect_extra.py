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
import threading
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
    """1つのサイトへの取得。リクエストの開始間隔を空け（interval 秒）、同時に開く接続数を max_conc までに抑える。
    失敗（通信エラー・5xx）は3回まで再試行する。"""

    def __init__(self, interval: float, max_conc: int = 1):
        self.interval = interval
        self.local = threading.local()
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.sem = threading.Semaphore(max_conc)

    def _session(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
            self.local.s.headers.update({"User-Agent": UA, "Accept-Language": "ja"})
        return self.local.s

    def _wait_slot(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_slot)
            self.next_slot = t + self.interval
        if t > now:
            time.sleep(t - now)

    def get(self, url: str):
        err = None
        with self.sem:
            for attempt in range(3):
                self._wait_slot()
                try:
                    r = self._session().get(url, timeout=30)
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
    """KドリームスのレースID＝場2桁＋開催初日8桁＋日目2桁＋00＋R2桁。
    日目は「開催初日から数えた暦の日数」（順延があると netkeirin の nichiji とずれるため、日付から計算する）。"""
    rid = race["race_id"]
    slug = VENUES.get(rid[8:10])
    first = race.get("kaisai_date_1st") or ""
    if not slug or len(first) != 8:
        return None
    try:
        day = (datetime.strptime(rid[:8], "%Y%m%d") - datetime.strptime(first, "%Y%m%d")).days + 1
    except ValueError:
        return None
    if not 1 <= day <= 9:
        return None
    return f"https://keirin.kdreams.jp/{slug}/racedetail/{rid[8:10]}{first}{day:02d}00{rid[10:12]}/"


def k_page_ok(race_id: str, data: dict) -> bool:
    """取得したページが、そのレース（日付とレース番号）のものかを確かめる。"""
    t = (data or {}).get("page_title") or ""
    return f"{race_id[:4]}年{race_id[4:6]}月{race_id[6:8]}日" in t and f" {int(race_id[10:12])}R " in t


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
                if r["type"].startswith("k") and not k_page_ok(r["race_id"], r.get("data")):
                    continue  # 別のレースのページだった記録は、取り直す
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
    ap.add_argument("--interval", type=float, default=0.4, help="同じサイトへのリクエストの開始間隔（秒）")
    ap.add_argument("--conc", type=int, default=3, help="同じサイトへ同時に開く接続数")
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
    fetchers = {"k": Fetcher(a.interval, a.conc), "op": Fetcher(a.interval, a.conc)}  # サイトごとに別々に制御
    pool = ThreadPoolExecutor(max_workers=2 * a.conc)
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
        targets = []
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
            for kind, url, parser in (("k", kdreams_url(race), parse_kdreams), ("op", oddspark_url(race), parse_oddspark)):
                if (kind, rid) not in have:
                    targets.append((race, kind, url, parser))
        batch = 3 * a.conc  # 何件かずつまとめて並行に取得し、書き込みは元の順番で行う
        for i in range(0, len(targets), batch):
            if (time.monotonic() - t0) / 60 > a.max_minutes:
                status("time_limit", new=n_new, errors=n_err, at=ds)
                print(json.dumps({"stopped": "time limit", "new": n_new, "errors": n_err, "at": ds}))
                return 0
            chunk = targets[i:i + batch]
            futs = [pool.submit(fetchers[kind].get, url) if url else None for _, kind, url, _ in chunk]
            for (race, kind, url, parser), fut in zip(chunk, futs):
                rid = race["race_id"]
                if url is None:
                    write({"type": "error", "what": kind, "race_id": rid, "error": "no url (venue or kaisai info missing)"})
                    n_err += 1
                    continue
                code, body = fut.result()
                now = datetime.now(JST).isoformat(timespec="microseconds")
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
                if kind == "k" and not k_page_ok(rid, data):
                    write({"type": "error", "what": kind, "race_id": rid, "url": url,
                           "error": "page mismatch: " + str(data.get("page_title"))[:120]})
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
