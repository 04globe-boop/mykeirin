"""netkeirin の過去データ（hist_YYYYMM.jsonl.gz）から、選手ごとの並びと市場の評価を取り出す。

出力（gzip CSV、1選手1行）：
  race_id, car, n_cars, scratched
  line_pos   head（ラインの先頭）/ second（番手）/ third（3番手）/ fourth_plus / single（単騎）
  line_size  自分のラインの車数（欠車を除く）
  n_lines    レース全体のライン数（単騎も1と数える）
  seri       番手などを競る予想になっているか（並び予想の上の段に載っている選手と、その真下の選手）
  mkt_win, mkt_top2, mkt_top3   確定3連単オッズから計算した市場の確率（1/オッズを全組で正規化して足し上げ）
  odds_official                 確定オッズの時刻（空なら確定前のオッズ）

並び予想の読み方（D1）：lineForecast の各行で syaban "0" はラインの区切り。
行が2つあるときは、車番が多い行が本来の並びで、もう一方の行の選手は、同じ位置（真下）の選手と競る予想。
欠車の選手（2車単の関係する目のオッズがすべて0）は並びから除き、後ろの選手を繰り上げる（D8）。
競る相手が欠車のときは、繰り上がった選手と競るものとして扱う（推定。P7、2026-10-19 京王閣8Rの例）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
from collections import defaultdict


def scratched_cars(odds: dict) -> set[str]:
    l6 = (odds or {}).get("list_6") or []
    cars = {x[0][:2] for x in l6} | {x[0][2:4] for x in l6}
    out = set()
    for c in cars:
        rel = [x for x in l6 if c in (x[0][:2], x[0][2:4])]
        if rel and all(float(x[1] or 0) == 0 for x in rel):
            out.add(str(int(c)))
    return out


def line_features(lines_raw: dict, scratched: set[str]) -> dict[str, dict]:
    rows = [[str(x.get("syaban", "0")) for x in row] for row in ((lines_raw or {}).get("lineForecast") or [])]
    if not rows:
        return {}
    main = max(rows, key=lambda r: sum(1 for x in r if x != "0"))
    others = [r for r in rows if r is not main]
    # 本来の並びをラインに分ける（位置の番号も覚えておく）
    lines, cur = [], []
    pos_of_index = {}
    for j, s in enumerate(main):
        if s == "0":
            if cur:
                lines.append(cur)
            cur = []
        else:
            cur.append(s)
            pos_of_index[j] = (len(lines), len(cur) - 1)
    if cur:
        lines.append(cur)
    seri_pairs = []
    for r in others:
        for j, s in enumerate(r):
            if s != "0" and j in pos_of_index:
                seri_pairs.append((s, j))
    # 欠車を除いて位置を数え直す
    feat = {}
    clean = [[c for c in ln if c not in scratched] for ln in lines]
    clean = [ln for ln in clean if ln]
    n_lines = len(clean)
    for ln in clean:
        for k, c in enumerate(ln):
            pos = "single" if len(ln) == 1 else ("head", "second", "third")[k] if k < 3 else "fourth_plus"
            feat[c] = {"line_pos": pos, "line_size": len(ln), "n_lines": n_lines, "seri": 0}
    for s, j in seri_pairs:
        if s in scratched:
            continue
        li, k = pos_of_index[j]
        ln = [c for c in lines[li] if c not in scratched]
        idx = len([c for c in lines[li][:k] if c not in scratched])  # 競る位置（欠車を詰めた後の何番目か）
        if idx < len(ln):
            # その位置にいる選手（相手が欠車なら、繰り上がった選手）と競る（P7）
            occupant = ln[idx]
            feat[s] = dict(feat[occupant], seri=1)
            feat[occupant]["seri"] = 1
        else:
            pos = "single" if not ln else ("head", "second", "third")[idx] if idx < 3 else "fourth_plus"
            feat[s] = {"line_pos": pos, "line_size": len(ln) + 1, "n_lines": n_lines, "seri": 0}
    return feat


def market(odds: dict) -> dict[str, dict]:
    l9 = [(x[0], float(x[1] or 0)) for x in (odds or {}).get("list_9") or []]
    l9 = [(c, o) for c, o in l9 if o > 0]
    if not l9:
        return {}
    tot = sum(1 / o for _, o in l9)
    acc = defaultdict(lambda: [0.0, 0.0, 0.0])
    for c, o in l9:
        p = (1 / o) / tot
        a, b, d = str(int(c[:2])), str(int(c[2:4])), str(int(c[4:6]))
        acc[a][0] += p
        for x in (a, b):
            acc[x][1] += p
        for x in (a, b, d):
            acc[x][2] += p
    return {k: {"mkt_win": v[0], "mkt_top2": v[1], "mkt_top3": v[2]} for k, v in acc.items()}


COLS = ["race_id", "car", "n_cars", "scratched", "line_pos", "line_size", "n_lines", "seri",
        "mkt_win", "mkt_top2", "mkt_top3", "odds_official"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("hist_glob")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    races, tosu = {}, {}
    for p in sorted(glob.glob(a.hist_glob)):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "race" and not r.get("errors"):
                    races[r["race_id"]] = r
                elif r.get("type") == "racelist":
                    for x in r.get("data") or []:
                        tosu[x["race_id"]] = x.get("tosu")
    n_rows = n_noline = 0
    with gzip.open(a.out, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for rid, r in sorted(races.items()):
            od = r.get("odds") or {}
            sc = scratched_cars(od)
            lf = line_features(r.get("lines") if isinstance(r.get("lines"), dict) else {}, sc)
            mk = market(od)
            cars = sorted({str(int(e["syaban"])) for e in (r.get("entries") or []) if str(e.get("syaban", "")).isdigit()}, key=int)
            for c in cars:
                row = {"race_id": rid, "car": c, "n_cars": tosu.get(rid), "scratched": int(c in sc),
                       "odds_official": od.get("official_dt") or ""}
                row.update(lf.get(c, {}))
                row.update({k: round(v, 6) for k, v in mk.get(c, {}).items()})
                if c not in sc and c not in lf:
                    n_noline += 1
                w.writerow(row)
                n_rows += 1
    print(json.dumps({"races": len(races), "rows": n_rows, "running_without_line": n_noline}))


if __name__ == "__main__":
    main()
