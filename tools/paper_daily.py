"""買ったつもり帳の毎朝の更新（データを読むだけ。収集・研究のワークフローやReleasesは変更しない）。

1. 2026-10-19〜昨日（JST）のうち、日別ファイルがない日・結果待ち／発走前が残る日・封印が解けた封印中の日を選ぶ
2. mykeirin2 の Releases から、その日のロガー（odds-YYYY-MM/odds_YYYYMMDD.jsonl.gz）と結果（extra/extra_YYYYMM.jsonl.gz）を読む
3. tools/paper_trade.py で日別ファイル（claude_track/paper/rows_YYYYMMDD.json）を作り直す
4. tools/paper_flat.py でダッシュボード用の paper_rows.json を作る

使い方：python tools/paper_daily.py --paper-dir ../mykeirin2/claude_track/paper --work /tmp/paper --out /tmp/paper/paper_rows.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_flat  # noqa: E402
import paper_trade  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
REPO = "04globe-boop/mykeirin2"
FIRST = date(2026, 10, 19)
REDO = {"結果待ち", "発走前"}


def asset(tag: str, name: str, dest: str) -> bool:
    if os.path.exists(dest):
        return True
    r = subprocess.run(["gh", "api", f"repos/{REPO}/releases/tags/{tag}", "--jq", f'.assets[] | select(.name=="{name}") | .id'],
                       capture_output=True, text=True)
    aid = r.stdout.strip()
    if not aid:
        return False
    with open(dest, "wb") as f:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", f"repos/{REPO}/releases/assets/{aid}"], stdout=f, check=True)
    return True


def needs(path: str, d: date, now: datetime) -> bool:
    if not os.path.exists(path):
        return True
    rows = json.load(open(path, encoding="utf-8"))
    st = {r.get("status") for r in rows}
    if st & REDO:
        return True
    return "封印中" in st and now >= paper_trade.UNSEAL_AT


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mult", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mykeirin2", "claude_track", "proto", "proto_v0.js"))
    a = ap.parse_args(argv)
    os.makedirs(a.work, exist_ok=True)
    os.makedirs(a.paper_dir, exist_ok=True)
    now = datetime.now(JST)
    last = now.date() - timedelta(days=1)
    d = FIRST
    report = []
    while d <= last:
        ds = d.strftime("%Y%m%d")
        out = os.path.join(a.paper_dir, f"rows_{ds}.json")
        if needs(out, d, now):
            odds = os.path.join(a.work, f"odds_{ds}.jsonl.gz")
            extra = os.path.join(a.work, f"extra_{ds[:6]}.jsonl.gz")
            if not asset(f"odds-{ds[:4]}-{ds[4:6]}", f"odds_{ds}.jsonl.gz", odds):
                report.append(f"{ds}: ロガーのファイルなし")
                d += timedelta(days=1)
                continue
            asset("extra", f"extra_{ds[:6]}.jsonl.gz", extra)
            nk = os.path.join(a.work, f"nk_results_{ds[:6]}.jsonl.gz")
            asset("nk-results", f"nk_results_{ds[:6]}.jsonl.gz", nk)  # Kドリームスが取れないときの代わり（なければ使わない）
            rows = paper_trade.build(ds, odds, extra, a.mult, None, now, nk if os.path.exists(nk) else None)
            json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False)
            from collections import Counter
            report.append(f"{ds}: " + ", ".join(f"{k} {v}" for k, v in Counter(r["status"] for r in rows).items()))
        d += timedelta(days=1)
    paper_flat.main(["--dir", a.paper_dir, "--out", a.out])
    print("\n".join(report) or "更新する日なし")


if __name__ == "__main__":
    main()
