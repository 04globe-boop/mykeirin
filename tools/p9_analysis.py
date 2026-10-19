"""P9（前走の負け方と次走の調子）の検証。計画は mykeirin2/research/p9_design.md。

使い方：python tools/p9_analysis.py --runners kdata_out/runners.csv.gz --out p9_result.json [--final]
  --final を付けない限り、最終テスト期間（2026-01〜）は読み込んだ直後に捨てる。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hashirihyo_dict import CATEGORIES, classify  # noqa: E402

TRAIN = ("2022-01-01", "2024-12-31")
DEV = ("2025-01-01", "2025-12-31")
TEST = ("2026-01-01", "2026-08-31")
STYLE = False  # 脚質（逃・両・追）を基準に入れるか。--style で有効（結果を見た後に加えた比較。最終テストでのみ判定）


def load(path: str):
    races = defaultdict(list)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            races[r["nk_race_id"]].append(r)
    return races


def build(races, final: bool):
    # レースごとの得点の並び
    hist = defaultdict(list)  # 選手 -> [(日付, レースID, 行)]
    field = {}
    for rid, rs in races.items():
        sc = []
        for r in rs:
            try:
                sc.append(float(r["race_score"]))
            except ValueError:
                sc.append(np.nan)
        arr = np.array(sc)
        field[rid] = arr
        for i, r in enumerate(rs):
            key = (r["player_name"].replace(" ", "").replace("　", ""), r["term"])
            hist[key].append((r["race_date"], int(r["race_no"]), rid, i))
    rows = []
    for key, lst in hist.items():
        lst.sort()
        finishes = []
        for j in range(len(lst) - 1):
            d0, _, rid0, i0 = lst[j]
            r0 = races[rid0][i0]
            rk0 = r0["rank"]
            finishes.append(int(rk0) if rk0.isdigit() else 9)
            wl = r0["win_lose"].strip()
            d1, rno1, rid1, i1 = lst[j + 1]
            r1 = races[rid1][i1]
            if not wl or not rk0:
                continue
            rk1 = r1["rank"].strip()
            if not rk1 or rk1 in ("欠",):
                continue
            if d1 >= TEST[0] and not final:
                continue
            arr = field[rid1]
            s = arr[i1]
            if np.isnan(s) or np.all(np.isnan(arr)):
                continue
            n = len(arr)
            rank_in = 1 + int(np.sum(arr[~np.isnan(arr)] > s))
            gap = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
            last3 = finishes[-3:]
            rows.append({
                "date": d1, "race": rid1, "y3": int(rk1.isdigit() and int(rk1) <= 3), "y1": int(rk1 == "1"),
                "wl": wl, "cat": classify(wl),
                "f": {
                    "score_rank": rank_in / n, "score_gap_max": float(np.nanmax(arr) - s), "score_gap_mean": float(np.nanmean(arr) - s),
                    "cls_" + r1["player_class"][:1]: 1.0, "n_" + str(n): 1.0, "rno_" + ("early" if int(r1["race_no"]) <= 7 else "late"): 1.0,
                    "log_gap": math.log1p(gap), "same_meet": float(gap <= 1 and r0["venue_code"] == r1["venue_code"]),
                    "prev_" + (rk0 if rk0.isdigit() else "x"): 1.0, "last3_mean": float(np.mean(last3)) / 9,
                    **({"style_" + (r1["running_style"] or "na"): 1.0} if STYLE else {}),
                },
            })
    return rows


def matrix(rows, names=None):
    keys = names or sorted({k for r in rows for k in r["f"]})
    idx = {k: i for i, k in enumerate(keys)}
    data, ri, ci = [], [], []
    for i, r in enumerate(rows):
        for k, v in r["f"].items():
            if k in idx:
                ri.append(i); ci.append(idx[k]); data.append(v)
    return sparse.csr_matrix((data, (ri, ci)), shape=(len(rows), len(keys))), keys


def logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def boot_ci(per_row_diff, race_ids, n_boot=1000, seed=0):
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(race_ids, return_inverse=True)
    sums = np.bincount(inv, weights=per_row_diff)
    cnts = np.bincount(inv)
    stats = []
    for _ in range(n_boot):
        s = rng.integers(0, len(uniq), len(uniq))
        stats.append(sums[s].sum() / cnts[s].sum())
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runners", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--style", action="store_true")
    a = ap.parse_args(argv)
    global STYLE
    STYLE = a.style
    rows = build(load(a.runners), a.final)
    tr = [r for r in rows if TRAIN[0] <= r["date"] <= TRAIN[1]]
    ev_range = TEST if a.final else DEV
    fit = tr + ([r for r in rows if DEV[0] <= r["date"] <= DEV[1]] if a.final else [])
    ev = [r for r in rows if ev_range[0] <= r["date"] <= ev_range[1]]
    out = {"n_fit": len(fit), "n_eval": len(ev), "eval_period": ev_range, "results": {}}

    Xf0, keys = matrix(fit)
    Xe0, _ = matrix(ev, keys)
    cats = [c for c in CATEGORIES if c != "role"]  # role を基準の水準にする
    def catm(rs):
        return sparse.csr_matrix(np.array([[1.0 if r["cat"] == c else 0.0 for c in cats] for r in rs]))
    vec = CountVectorizer(analyzer="char", ngram_range=(1, 3), min_df=30)
    Tf = vec.fit_transform([r["wl"] for r in fit])
    Te = vec.transform([r["wl"] for r in ev])
    race_ids = np.array([r["race"] for r in ev])
    for target in ("y3", "y1"):
        yf = np.array([r[target] for r in fit])
        ye = np.array([r[target] for r in ev])
        res = {}
        preds = {}
        for name, Xf, Xe, C in (("B0", Xf0, Xe0, 1.0),
                                ("M1", sparse.hstack([Xf0, catm(fit)]).tocsr(), sparse.hstack([Xe0, catm(ev)]).tocsr(), 1.0),
                                ("M2", sparse.hstack([Xf0, Tf]).tocsr(), sparse.hstack([Xe0, Te]).tocsr(), 0.05)):
            m = LogisticRegression(C=C, max_iter=3000)
            m.fit(Xf, yf)
            p = m.predict_proba(Xe)[:, 1]
            preds[name] = p
            res[name] = {"logloss": float(logloss(ye, p).mean()), "brier": float(np.mean((p - ye) ** 2))}
        for name in ("M1", "M2"):
            d = logloss(ye, preds["B0"]) - logloss(ye, preds[name])  # プラスなら改善
            lo, hi = boot_ci(d, race_ids)
            res[name]["logloss_improve_vs_B0"] = float(d.mean())
            res[name]["improve_ci95"] = [lo, hi]
        # 分類ごとの「実際−基準の予想」
        eff = {}
        for c in CATEGORIES:
            mask = np.array([r["cat"] == c for r in ev])
            if mask.sum() < 50:
                continue
            diff = ye[mask] - preds["B0"][mask]
            lo, hi = boot_ci(diff, race_ids[mask])
            eff[c] = {"n": int(mask.sum()), "actual": float(ye[mask].mean()), "expected": float(preds["B0"][mask].mean()),
                      "diff": float(diff.mean()), "ci95": [lo, hi]}
        res["effect_by_category"] = eff
        # 較正（基準と M2 を10分位で）
        cal = {}
        for name in ("B0", "M2"):
            p = preds[name]
            q = np.quantile(p, np.linspace(0, 1, 11))
            b = np.clip(np.searchsorted(q, p, side="right") - 1, 0, 9)
            cal[name] = [[float(p[b == i].mean()), float(ye[b == i].mean())] for i in range(10) if (b == i).any()]
        res["calibration_deciles"] = cal
        out["results"][target] = res
    # 文字の並びで、効果の大きいもの（3着以内、M2を学習データで）
    yf = np.array([r["y3"] for r in fit])
    m = LogisticRegression(C=0.05, max_iter=3000).fit(sparse.hstack([Xf0, Tf]).tocsr(), yf)
    coef = m.coef_[0][Xf0.shape[1]:]
    voc = np.array(vec.get_feature_names_out())
    o = np.argsort(coef)
    out["ngram_most_negative"] = [[voc[i], round(float(coef[i]), 3)] for i in o[:25]]
    out["ngram_most_positive"] = [[voc[i], round(float(coef[i]), 3)] for i in o[-25:][::-1]]
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "results"}, ensure_ascii=False)[:600])
    for t, res in out["results"].items():
        print(t, {k: res[k] for k in ("B0", "M1", "M2")})
        for c, e in res["effect_by_category"].items():
            print("  ", c, e)


if __name__ == "__main__":
    main()
