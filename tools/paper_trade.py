"""買ったつもりの記録（ペーパートレード）。研究の収集・学習には触れず、保存済みのデータを読むだけ。

1日分について、ロガーの締切5分前のオッズと7分前の並び予想から、決めておいた4つの買い方の「買い目」を作り、
Kドリームスの結果ページ（extra収集の type=k）の払戻で当たり外れと収支を付ける。

買い方（2026-10-20に固定。結果を見て変えない）：
  A 市場の本命   … 3連単オッズで一番人気の目を1点（100円）
  B 補正の本命   … Claude側プロトタイプv0の補正後で一番確率の高い目を1点
  C 補正の上位5点 … 補正後の確率の上位5点（500円）
  D ライン独占狙い … 市場の人気30位以内で、補正が最も確率を上げた目を1点（同率なら補正後の確率が高い方）

封印：2026-11-21〜2027-01-19のレースは、2027-01-20 03:00 JSTまで買い目だけを記録し、結果は付けない
（Codex側の未閲覧試験期間。市場補正の成績を先に見ないため）。

使い方：
  python tools/paper_trade.py --date 20261019 --odds odds_20261019.jsonl.gz --extra extra_202610.jsonl.gz --out rows.json
  （--results-json で、netkeirinの結果ページから取った当日分の結果を一時的に使える）
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hist_features import scratched_cars  # noqa: E402
from market_scan import band_of, implied, line_ids, structure  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
SEAL_START, SEAL_END = "20261121", "20270119"
UNSEAL_AT = datetime(2027, 1, 20, 3, 0, tzinfo=JST)
STRATEGIES = {"A": "市場の本命", "B": "補正の本命", "C": "補正の上位5点", "D": "ライン独占狙い"}
TRI_RE = re.compile(r"(\d+)-(\d+)-(\d+)\s*([\d,]+)円")


def load_mult(path: str) -> dict:
    js = open(path, encoding="utf-8").read()
    return json.loads(re.search(r"const KPROTO_MULT = (\{.*?\});", js).group(1))


def read_gz(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def latest(recs, rid, typ, label):
    xs = [r for r in recs if r.get("race_id") == rid and r.get("type") == typ and r.get("label") == label]
    return xs[-1] if xs else None


def picks(od: dict, lines_raw: dict, mult: dict):
    p = implied(od.get("list_9"), 3)
    if not p:
        return None
    feat = line_ids(lines_raw if isinstance(lines_raw, dict) else {}, scratched_cars(od))
    ranked = sorted(p.items(), key=lambda x: -x[1])
    rank = {c: r for r, (c, _) in enumerate(ranked, 1)}
    q = {}
    for c, v in ranked:
        st = structure(*c, feat) if feat else None
        q[c] = v * (mult.get(f"{st[0]}|{st[1]}|{band_of(rank[c])}", 1.0) if st else 1.0)
    z = sum(q.values())
    q = {c: v / z for c, v in q.items()}
    odds = {}
    for x in od.get("list_9") or []:
        try:
            o = float(x[1])
        except (TypeError, ValueError):
            continue
        if o > 0:
            odds[tuple(str(int(x[0][i:i + 2])) for i in (0, 2, 4))] = o
    byq = sorted(q, key=lambda c: -q[c])
    cand = [c for c in q if rank[c] <= 30]
    d = max(cand, key=lambda c: (q[c] / p[c], q[c]))
    return {"A": [ranked[0][0]], "B": [byq[0]], "C": byq[:5], "D": [d]}, p, q, odds, rank, bool(feat)


def result_of(extra: list[dict], rid: str):
    ks = [r for r in extra if r.get("type") == "k" and str(r.get("race_id")) == rid and (r.get("data") or {}).get("result")]
    if not ks:
        return None
    data = ks[-1]["data"]
    text = " ".join(str(c) for row in (data.get("refund") or []) for c in row)
    # 3連単は「a-b-c 金額円」、3連複は「a=b=c」、2車単は「a-b」なので、数字3つを「-」でつないだものだけが3連単
    tri = {}
    for m in TRI_RE.finditer(text):
        tri[(str(int(m.group(1))), str(int(m.group(2))), str(int(m.group(3))))] = int(m.group(4).replace(",", ""))
    if not tri:
        return {"status": "返還・不成立" if ("返還" in text or "不成立" in text) else "払戻なし"}
    return {"status": "確定", "wins": tri}


def build(date: str, odds_path: str, extra_path: str, mult_path: str, results_json: str | None, now: datetime):
    mult = load_mult(mult_path)
    recs = read_gz(odds_path)
    extra = read_gz(extra_path)
    override = json.load(open(results_json)) if results_json else {}
    races = {}
    for r in recs:
        if r.get("type") == "racelist":
            for v in r.get("data") or []:
                if v.get("race_id", "").startswith(date):
                    races[v["race_id"]] = v
    sealed = SEAL_START <= date <= SEAL_END and now < UNSEAL_AT
    rows = []
    for rid in sorted(races):
        meta = races[rid]
        row = {"race_id": rid, "date": f"{date[:4]}-{date[4:6]}-{date[6:]}", "venue": meta.get("jyo"), "race_no": int(rid[-2:]),
               "start": meta.get("start"), "grade": meta.get("jyoken"), "n_cars": int(meta.get("tosu") or 0)}
        od = latest(recs, rid, "odds", "-5")
        ln = latest(recs, rid, "lines", "-7") or latest(recs, rid, "lines", "-5") or latest(recs, rid, "lines", "-60")
        pk = picks(od["data"], ln["data"] if ln else {}, mult) if od and isinstance(od.get("data"), dict) else None
        if not pk:
            start = datetime.strptime(date + (meta.get("start") or "23:59"), "%Y%m%d%H:%M").replace(tzinfo=JST)
            row.update(status="発走前" if start > now else "記録なし", bets={})
            rows.append(row)
            continue
        sel, p, q, odds, rank, has_lines = pk
        row["has_lines"] = has_lines
        row["odds_at"] = od.get("fetched_at")
        row["bets"] = {k: [{"combo": "-".join(c), "odds_t5": odds.get(c), "rank": rank[c], "p_mkt": round(p[c], 5), "p_adj": round(q[c], 5)}
                           for c in v] for k, v in sel.items()}
        if sealed:
            row["status"] = "封印中"
            rows.append(row)
            continue
        res = None
        if rid in override:
            tri, pay = override[rid][0], override[rid][1]
            res = {"status": "確定", "wins": {tuple(tri.replace(">", "-").split("-")): int(pay)}, "source": "netkeirin"}
        else:
            res = result_of(extra, rid)
            if res:
                res["source"] = "kdreams"
        if not res:
            row["status"] = "結果待ち"
        elif res["status"] != "確定":
            row["status"] = res["status"]
        else:
            row["status"] = "確定"
            row["result_source"] = res["source"]
            wins = res["wins"]
            row["result"] = ["-".join(c) for c in wins]
            row["payout"] = max(wins.values())
            first = next(iter(wins))
            row["result_rank_mkt"] = rank.get(first)
            row["p_mkt_result"] = round(p.get(first, 0), 6)
            row["p_adj_result"] = round(q.get(first, 0), 6)
            row["outcome"] = {}
            for k, v in sel.items():
                ret = sum(wins.get(c, 0) for c in v)
                row["outcome"][k] = {"cost": 100 * len(v), "return": ret, "hit": ret > 0}
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--odds", required=True)
    ap.add_argument("--extra", default="")
    ap.add_argument("--mult", default=os.path.join(os.path.dirname(__file__), "..", "..", "mykeirin2", "claude_track", "proto", "proto_v0.js"))
    ap.add_argument("--results-json", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows = build(a.date, a.odds, a.extra, a.mult, a.results_json, datetime.now(JST))
    json.dump(rows, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    from collections import Counter
    print(a.date, len(rows), Counter(r["status"] for r in rows))
    for k, name in STRATEGIES.items():
        done = [r for r in rows if r.get("status") == "確定"]
        cost = sum(r["outcome"][k]["cost"] for r in done)
        ret = sum(r["outcome"][k]["return"] for r in done)
        hit = sum(r["outcome"][k]["hit"] for r in done)
        if done:
            print(f"  {k} {name}: {hit}/{len(done)} 的中 回収率 {ret / cost:.1%}（{ret - cost:+,}円）")


if __name__ == "__main__":
    main()
