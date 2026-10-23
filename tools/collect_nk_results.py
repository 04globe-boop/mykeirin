"""netkeirin の結果ページ（着順・払戻）の収集。Kドリームスが取れないときの代わりの出典（Claude側・独立）。

既存の収集（logger/ の collect_history・collect_extra・run_logger）とは別のファイル・別のリリースに保存し、
それらのファイル名・レコードの種類は変えない。

保存形式：<out-dir>/nk_results_YYYYMM.jsonl.gz（1行1レコード、gzip の追記）
  type=nk_result  1レース分：title、result（着順表の行ごとのセル）、payout（券種ごとの [組, 払戻円, 人気]）、status
                  status は「確定」（3連単の払戻あり）／「返還」／「未確定」（払戻表なし。次の実行で取り直す）
  type=error      通信エラーなど（推測で埋めない。次の実行で取り直す）

使い方：python tools/collect_nk_results.py --date 2026-10-21 [--date 2026-10-22 ...] --out-dir nk
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logger"))
from netkeirin import HEADERS, NetkeirinClient  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
RESULT_URL = "https://keirin.netkeiba.com/race/result/?race_id={}"
BET_TYPES = ("２車複", "２車単", "ワイド", "３連複", "３連単", "枠複", "枠単")


def _rows(table) -> list[list[str]]:
    out = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if any(cells):
            out.append(cells)
    return out


def parse_result_page(html: bytes | str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    res = soup.find("table", class_="ResultRefund")
    pay = soup.find("table", class_="Payout_Detail_Table")
    result = _rows(res) if res else []
    payout: dict[str, list] = {}
    refunded = False
    if pay:
        cur = None
        for cells in _rows(pay):
            if cells and cells[0] in BET_TYPES:
                cur = cells[0]
                cells = cells[1:]
            if not cur:
                continue
            if any("返還" in c for c in cells):
                refunded = True
                payout.setdefault(cur, [])
                continue
            vals = [c for c in cells if c]
            if len(vals) >= 2 and vals[1].endswith("円"):
                yen = int(re.sub(r"[^\d]", "", vals[1]) or 0)
                pop = int(re.sub(r"[^\d]", "", vals[2]) or 0) if len(vals) > 2 else None
                payout.setdefault(cur, []).append([vals[0], yen, pop])
    if payout.get("３連単"):
        status = "確定"
    elif refunded:
        status = "返還"
    else:
        status = "未確定"
    return {"title": title, "result": result, "payout": payout, "status": status}


def page_ok(race_id: str, data: dict) -> bool:
    t = data.get("title") or ""
    return f"{race_id[:4]}年{race_id[4:6]}月{race_id[6:8]}日" in t and f" {int(race_id[10:12])}R " in t


def done_keys(path: str) -> set[str]:
    out = set()
    if not os.path.exists(path):
        return out
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "nk_result" and (r.get("data") or {}).get("status") in ("確定", "返還"):
                out.add(r["race_id"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", required=True, help="YYYY-MM-DD（複数可）")
    ap.add_argument("--out-dir", default="nk")
    ap.add_argument("--interval", type=float, default=0.8)
    a = ap.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    client = NetkeirinClient()
    sess = requests.Session()
    sess.headers.update(HEADERS)
    now = datetime.now(JST)
    summary = {}
    for d in a.date:
        ds = d.replace("-", "")
        path = os.path.join(a.out_dir, f"nk_results_{ds[:6]}.jsonl.gz")
        done = done_keys(path)
        races = [r for r in client.race_list(ds) if str(r.get("race_id", "")).startswith(ds)]
        cnt = {"確定": 0, "返還": 0, "未確定": 0, "skip": 0, "error": 0, "not_started": 0}
        with gzip.open(path, "at", encoding="utf-8") as out:
            for r in sorted(races, key=lambda x: x["race_id"]):
                rid = r["race_id"]
                if rid in done:
                    cnt["skip"] += 1
                    continue
                start = r.get("start") or "23:59"
                if datetime.strptime(ds + start, "%Y%m%d%H:%M").replace(tzinfo=JST) > now:
                    cnt["not_started"] += 1
                    continue
                url = RESULT_URL.format(rid)
                time.sleep(a.interval)
                try:
                    resp = sess.get(url, timeout=20)
                    resp.raise_for_status()
                    data = parse_result_page(resp.content)
                    if not page_ok(rid, data):
                        raise ValueError("page mismatch: " + (data.get("title") or "")[:40])
                    rec = {"type": "nk_result", "race_id": rid, "url": url,
                           "fetched_at": datetime.now(JST).isoformat(timespec="microseconds"), "data": data}
                    cnt[data["status"]] += 1
                except Exception as e:  # noqa: BLE001
                    rec = {"type": "error", "race_id": rid, "url": url, "error": str(e)[:200],
                           "fetched_at": datetime.now(JST).isoformat(timespec="microseconds")}
                    cnt["error"] += 1
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        summary[d] = {"races": len(races), **cnt}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
