"""買ったつもりの記録（paper_trade.py の日別出力）を、ダッシュボード用に1レース1行の表へまとめる。

使い方：python tools/paper_flat.py --dir ../mykeirin2/claude_track/paper --out paper_rows.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os

KEEP = {"確定", "封印中", "結果待ち", "返還・不成立", "払戻なし"}


def flat(row: dict) -> dict:
    out = {k: row.get(k) for k in ("race_id", "date", "venue", "race_no", "start", "grade", "n_cars", "status")}
    out["label"] = f"{row['venue']} {row['race_no']}R"
    out["result"] = " / ".join(row.get("result") or []) or None
    out["payout"] = row.get("payout")
    out["result_rank"] = row.get("result_rank_mkt")
    out["p_mkt_result"] = row.get("p_mkt_result")
    out["p_adj_result"] = row.get("p_adj_result")
    bets, oc = row.get("bets") or {}, row.get("outcome") or {}
    for k in "ABCD":
        b = bets.get(k) or []
        out[f"{k}_combo"] = " / ".join(x["combo"] for x in b) or None
        out[f"{k}_odds"] = b[0]["odds_t5"] if len(b) == 1 else None
        out[f"{k}_rank"] = b[0]["rank"] if len(b) == 1 else None
        o = oc.get(k)
        out[f"{k}_cost"] = o["cost"] if o else None
        out[f"{k}_return"] = o["return"] if o else None
        out[f"{k}_hit"] = (1 if o["hit"] else 0) if o else None
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows = []
    for p in sorted(glob.glob(os.path.join(a.dir, "rows_*.json"))):
        rows += [flat(r) for r in json.load(open(p, encoding="utf-8")) if r.get("status") in KEEP]
    rows.sort(key=lambda r: (r["date"], r["start"] or "", r["race_id"]))
    json.dump(rows, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(len(rows), "races ->", a.out)


if __name__ == "__main__":
    main()
