"""過去レースの一括収集（netkeirin 内部API）。

1日ずつ、その日の全レース一覧（AplRace）を取り、各レースについて
確定オッズ（AplRaceOdds）、並び予想（AplNarabiYoso2）、出走表（AplRaceHorse）を取る。
すでに保存済みのレースは飛ばすので、途中で止まっても同じコマンドで再開できる。

保存形式：<out-dir>/hist_YYYYMM.jsonl.gz（1行1レコード、gzip の追記）
  type=racelist  その日の全レース一覧（開催初日 kaisai_date_1st・日目 nichiji を含む＝サイト間のID対応に使う）
  type=race      1レース分（odds・lines・entries をまとめて保存）
  type=error     通信エラーなど（推測で埋めない）
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netkeirin import NetkeirinClient, parse_lines  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


def month_days(ym: str) -> list[date]:
    y, m = (int(x) for x in ym.split("-"))
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += timedelta(days=1)
    return out


def done_races(path: str) -> tuple[set, set]:
    races, days = set(), set()
    if not os.path.exists(path):
        return races, days
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue  # 途中で切れた行は無視
            if r.get("type") == "race":
                races.add(r["race_id"])
            elif r.get("type") == "racelist":
                days.add(r["kaisai_date"])
    return races, days


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out-dir", default="hist")
    ap.add_argument("--max-minutes", type=float, default=330, help="この時間を超えたら区切りよく止める")
    ap.add_argument("--interval", type=float, default=0.8, help="リクエスト間隔（秒）")
    a = ap.parse_args(argv)

    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"hist_{a.month.replace('-', '')}.jsonl.gz")
    have, have_days = done_races(path)
    cl = NetkeirinClient(min_interval=a.interval)
    t0 = time.monotonic()
    n_new = n_err = 0
    today = datetime.now(JST).date()

    def write(rec):
        with gzip.open(path, "at", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    for d in month_days(a.month):
        if d >= today:
            break
        ds = d.strftime("%Y%m%d")
        try:
            races = cl.race_list(ds)
        except Exception as e:  # noqa: BLE001
            write({"type": "error", "what": "racelist", "kaisai_date": ds, "error": str(e)[:300]})
            n_err += 1
            continue
        if ds not in have_days:
            write({"type": "racelist", "kaisai_date": ds, "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
                   "data": races})
        for r in races:
            rid = r.get("race_id")
            if not rid or rid in have:
                continue
            if (time.monotonic() - t0) / 60 > a.max_minutes:
                print(json.dumps({"stopped": "time limit", "new": n_new, "errors": n_err, "at": ds}, ensure_ascii=False))
                return 0
            rec = {"type": "race", "race_id": rid, "kaisai_date": ds,
                   "fetched_at": datetime.now(JST).isoformat(timespec="seconds")}
            ok = True
            for key, fn in (("odds", cl.odds), ("lines", cl.lines), ("entries", cl.entries)):
                try:
                    rec[key] = fn(rid)
                except Exception as e:  # noqa: BLE001
                    rec[key] = None
                    rec.setdefault("errors", {})[key] = str(e)[:300]
                    ok = False
            if isinstance(rec.get("lines"), dict):
                rec["lines_parsed"] = parse_lines(rec["lines"])
            write(rec)
            have.add(rid)
            n_new += 1
            n_err += 0 if ok else 1
        print(f"{ds}: races={len(races)} total_new={n_new} errors={n_err}", flush=True)
    print(json.dumps({"finished": a.month, "new": n_new, "errors": n_err}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
