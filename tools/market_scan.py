"""S1（3連単の構造別の較正）と S2（券種間のずれ）の点検。計画は mykeirin2/claude_track/s1s2_design.md。

python tools/market_scan.py --hist "hist/hist_2024*.jsonl.gz" ... --kdata-races kdata_out/races.csv.gz --out scan.json
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
from scipy.optimize import minimize
from scipy.stats import chi2, poisson

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hist_features import line_features, scratched_cars  # noqa: E402

DISCOVERY = ("20240201", "20241231")
CONFIRM = ("20250101", "20251231")
BANDS = [(1, 10), (11, 30), (31, 100), (101, 200), (201, 10 ** 6)]


def band_of(rank):
    for i, (a, b) in enumerate(BANDS):
        if a <= rank <= b:
            return i
    return len(BANDS) - 1


def implied(lst, k):
    """オッズの一覧（[組, オッズ, ...]）から、正規化した確率 {組の車番の組: p}。"""
    vals = []
    for x in lst or []:
        try:
            o = float(x[1])
        except (TypeError, ValueError):
            continue
        if o > 0:
            c = x[0]
            vals.append((tuple(str(int(c[i:i + 2])) for i in range(0, 2 * k, 2)), 1 / o))
    s = sum(v for _, v in vals)
    return {c: v / s for c, v in vals} if s else {}


def structure(i, j, k, feat):
    li = {c: (f["line_id"], f["line_pos"]) for c, f in feat.items()}
    if any(x not in li for x in (i, j, k)):
        return None
    (gi, pi), (gj, pj), (gk, _) = li[i], li[j], li[k]
    if pi == "single" or pj == "single":
        a = "単騎を含む"
    elif gi == gj and pi == "second" and pj == "head":
        a = "同ライン番手→先頭"
    elif gi == gj and pi == "head" and pj == "second":
        a = "同ライン先頭→番手"
    elif gi == gj:
        a = "同ラインその他"
    else:
        a = "別ライン"
    b = "3着同ライン" if (gk == gi or gk == gj) and li[k][1] != "single" else "3着別線"
    return a, b


def load(hist_globs, kdata_races):
    truth = {}
    with gzip.open(kdata_races, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["tri_combo"] and r["tri_combo"].count("-") == 2:
                truth[r["nk_race_id"]] = tuple(str(int(x)) for x in r["tri_combo"].split("-"))
    races = {}
    for g in hist_globs:
        for p in sorted(glob.glob(g)):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    if r.get("type") == "race" and not r.get("errors"):
                        races[r["race_id"]] = r
    return races, truth


def line_ids(lines_raw, sc):
    feat = line_features(lines_raw, sc)
    # line_features は位置だけを返すので、ライン番号を付け直す（同じ先頭を共有する並び＝同じライン）
    rows = [[str(x.get("syaban", "0")) for x in row] for row in (lines_raw or {}).get("lineForecast") or []]
    if not rows:
        return {}
    main = max(rows, key=lambda r: sum(1 for x in r if x != "0"))
    gid, cur, idx_line = 0, False, {}
    for j, s in enumerate(main):
        if s == "0":
            if cur:
                gid += 1
            cur = False
        else:
            idx_line[j] = gid
            cur = True
    for c in feat:
        feat[c]["line_id"] = None
    for j, s in enumerate(main):
        if s != "0" and s in feat:
            feat[s]["line_id"] = idx_line[j]
    for r in rows:
        if r is main:
            continue
        for j, s in enumerate(r):
            if s != "0" and s in feat and j in idx_line:
                feat[s]["line_id"] = idx_line[j]
    return {c: f for c, f in feat.items() if f.get("line_id") is not None}


def s1(races, truth):
    out = {}
    excl = defaultdict(int)
    for period, (lo, hi) in (("discovery", DISCOVERY), ("confirm", CONFIRM)):
        E = defaultdict(float)
        O = defaultdict(int)
        n = defaultdict(int)
        byday = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
        for rid, r in races.items():
            if not (lo <= rid[:8] <= hi):
                continue
            od = r.get("odds") or {}
            if rid not in truth:
                excl["no_result"] += 1
                continue
            if not od.get("official_dt"):
                excl["no_official"] += 1
                continue
            sc = scratched_cars(od)
            feat = line_ids(r.get("lines") if isinstance(r.get("lines"), dict) else {}, sc)
            if not feat:
                excl["no_lines"] += 1
                continue
            p = implied(od.get("list_9"), 3)
            if not p or truth[rid] not in p:
                excl["result_not_in_odds"] += 1
                continue
            ranked = sorted(p.items(), key=lambda x: -x[1])
            for rank, (c, q) in enumerate(ranked, 1):
                st = structure(*c, feat)
                if st is None:
                    continue
                key = (st[0], st[1], band_of(rank))
                hit = int(c == truth[rid])
                E[key] += q
                O[key] += hit
                n[key] += 1
                byday[rid[:8]][key][0] += q
                byday[rid[:8]][key][1] += hit
        days = sorted(byday)
        rng = np.random.default_rng(0)
        res = {}
        for key in E:
            ratio = O[key] / E[key] if E[key] else float("nan")
            vec = np.array([[byday[d][key][0], byday[d][key][1]] for d in days])
            boots = []
            for _ in range(1000):
                s = vec[rng.integers(0, len(days), len(days))].sum(0)
                boots.append(s[1] / s[0] if s[0] else np.nan)
            b = np.array(boots)
            # 検定：市場が正しければ的中数は平均 E のポアソン分布に近い（組ごとの確率が小さいため）。両側の正確な p 値
            e, o = E[key], O[key]
            pval = float(min(1.0, 2 * min(poisson.cdf(o, e), poisson.sf(o - 1, e)))) if e > 0 else 1.0
            lo_ci = chi2.ppf(0.025, 2 * o) / 2 / e if o > 0 else 0.0
            hi_ci = chi2.ppf(0.975, 2 * (o + 1)) / 2 / e
            res["|".join([key[0], key[1], f"{BANDS[key[2]][0]}-{BANDS[key[2]][1] if BANDS[key[2]][1] < 10**6 else ''}"])] = {
                "combos": n[key], "expected": round(E[key], 2), "observed": O[key], "ratio": round(ratio, 3),
                "ci95": [round(float(lo_ci), 3), round(float(hi_ci), 3)],
                "ci95_dayboot": [round(float(np.nanpercentile(b, 2.5)), 3), round(float(np.nanpercentile(b, 97.5)), 3)],
                "p": round(float(pval), 6)}
        out[period] = res
    out["excluded"] = dict(excl)
    # 発見期間で BH（q=0.05）→ 確認期間で同じ向きか
    keys = sorted([k for k, v in out["discovery"].items() if v["expected"] >= 5], key=lambda k: out["discovery"][k]["p"])
    m = len(keys)
    passed = []
    for i, k in enumerate(keys, 1):
        if out["discovery"][k]["p"] <= 0.05 * i / m:
            passed = keys[:i]
    verdict = {}
    for k in passed:
        d, c = out["discovery"][k], out["confirm"].get(k)
        same = c is not None and ((d["ratio"] - 1) * (c["ratio"] - 1) > 0) and (c["ci95"][0] > 1 or c["ci95"][1] < 1)
        verdict[k] = {"discovery_ratio": d["ratio"], "confirm_ratio": c["ratio"] if c else None,
                      "confirm_ci95": c["ci95"] if c else None, "replicated": bool(same)}
    out["bh_discovery_passed"] = verdict
    return out


def pair_probs(od, kind):
    if kind == "exacta":
        own = implied(od.get("list_6"), 2)
        tri = defaultdict(float)
        for (a, b, _), q in implied(od.get("list_9"), 3).items():
            tri[(a, b)] += q
    else:
        own = {tuple(sorted(c, key=int)): q for c, q in implied(od.get("list_5"), 2).items()}
        tri = defaultdict(float)
        for (a, b, _), q in implied(od.get("list_9"), 3).items():
            tri[tuple(sorted((a, b), key=int))] += q
    keys = [k for k in own if tri.get(k, 0) > 0]
    return keys, np.array([own[k] for k in keys]), np.array([tri[k] for k in keys])


def s2(races, truth):
    out = {}
    for kind in ("exacta", "quinella"):
        data = {"discovery": [], "confirm": []}
        for rid, r in races.items():
            od = r.get("odds") or {}
            if rid not in truth or not od.get("official_dt"):
                continue
            per = "discovery" if DISCOVERY[0] <= rid[:8] <= DISCOVERY[1] else "confirm" if CONFIRM[0] <= rid[:8] <= CONFIRM[1] else None
            if per is None:
                continue
            keys, po, pt = pair_probs(od, kind)
            t = truth[rid]
            target = (t[0], t[1]) if kind == "exacta" else tuple(sorted((t[0], t[1]), key=int))
            if target not in keys:
                continue
            data[per].append((rid[:8], np.log(po), np.log(pt), keys.index(target)))

        def nll(params, rows, which):
            a, b = params
            tot = 0.0
            for _, lo, lt, y in rows:
                u = (a * lo if which in ("both", "own") else 0) + (b * lt if which in ("both", "tri") else 0)
                u = u - u.max()
                tot -= u[y] - math.log(np.exp(u).sum())
            return tot / len(rows)

        fits = {}
        for which, x0 in (("own", [1.0, 0.0]), ("tri", [0.0, 1.0]), ("both", [0.5, 0.5])):
            f = minimize(lambda v: nll(v, data["discovery"], which), x0, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-7})
            fits[which] = f.x.tolist()
        per_race = {}
        for which in ("own", "tri", "both"):
            a, b = fits[which]
            vals = []
            for _, lo, lt, y in data["confirm"]:
                u = (a * lo if which in ("both", "own") else 0) + (b * lt if which in ("both", "tri") else 0)
                u = u - u.max()
                vals.append(-(u[y] - math.log(np.exp(u).sum())))
            per_race[which] = np.array(vals)
        days = np.array([d for d, *_ in data["confirm"]])
        uniq, inv = np.unique(days, return_inverse=True)
        rng = np.random.default_rng(0)
        res = {"n_discovery": len(data["discovery"]), "n_confirm": len(data["confirm"]), "fits": fits,
               "confirm_nll": {k: float(v.mean()) for k, v in per_race.items()}}
        for base in ("own", "tri"):
            d = per_race[base] - per_race["both"]  # プラス＝融合が良い
            sums = np.bincount(inv, weights=d)
            cnts = np.bincount(inv)
            boots = []
            for _ in range(2000):
                s = rng.integers(0, len(uniq), len(uniq))
                boots.append(sums[s].sum() / cnts[s].sum())
            res[f"improve_vs_{base}"] = {"mean": float(d.mean()), "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}
        out[kind] = res
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", nargs="+", required=True)
    ap.add_argument("--kdata-races", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    races, truth = load(a.hist, a.kdata_races)
    races = {k: v for k, v in races.items() if DISCOVERY[0] <= k[:8] <= CONFIRM[1]}  # 取り決め：2024-02〜2025-12 だけ
    out = {"n_races_loaded": len(races), "s1": s1(races, truth), "s2": s2(races, truth)}
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps({"n": out["n_races_loaded"], "excluded": out["s1"]["excluded"],
                      "bh": out["s1"]["bh_discovery_passed"], "s2": out["s2"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()


def followup(races, truth, out_s1):
    """結果を見た後の追加分析（2024で決めて2025で評価）。
    F1 構造×人気帯の比で市場の3連単確率を補正したときの、2025年の3連単の対数損失の変化
    F2 構造×人気帯ごとの、確定オッズでの単純回収率（参考）
    F3 券種間のずれ：3連単合計の確率と2車単・2車複の確率の比 r が大きい組を、その券種で買った場合の回収率（2024でしきい値を決め、2025で評価。参考）
    """
    w = {}
    for k, v in out_s1["discovery"].items():
        a, b, band = k.split("|")
        w[(a, b, band)] = (v["observed"] + 1) / (v["expected"] + 1)  # 小さい区分は1に縮める
    band_name = lambda i: f"{BANDS[i][0]}-{BANDS[i][1] if BANDS[i][1] < 10**6 else ''}"
    nll_raw, nll_adj, days = [], [], []
    roi = defaultdict(lambda: [0.0, 0])
    for rid, r in races.items():
        if not (CONFIRM[0] <= rid[:8] <= CONFIRM[1]) or rid not in truth:
            continue
        od = r.get("odds") or {}
        if not od.get("official_dt"):
            continue
        sc = scratched_cars(od)
        feat = line_ids(r.get("lines") if isinstance(r.get("lines"), dict) else {}, sc)
        p = implied(od.get("list_9"), 3)
        if not feat or truth[rid] not in p:
            continue
        odds = {tuple(str(int(x[0][i:i + 2])) for i in (0, 2, 4)): float(x[1]) for x in od["list_9"] if float(x[1] or 0) > 0}
        ranked = sorted(p.items(), key=lambda x: -x[1])
        adj = {}
        for rank, (c, q) in enumerate(ranked, 1):
            st = structure(*c, feat)
            m = w.get((st[0], st[1], band_name(band_of(rank))), 1.0) if st else 1.0
            adj[c] = q * m
            if st:
                key = (st[0], st[1])
                roi[key][0] += odds[c] if c == truth[rid] else 0.0
                roi[key][1] += 1
        z = sum(adj.values())
        nll_raw.append(-math.log(p[truth[rid]]))
        nll_adj.append(-math.log(adj[truth[rid]] / z))
        days.append(rid[:8])
    d = np.array(nll_raw) - np.array(nll_adj)  # プラス＝補正で改善
    uniq, inv = np.unique(np.array(days), return_inverse=True)
    sums, cnts = np.bincount(inv, weights=d), np.bincount(inv)
    rng = np.random.default_rng(1)
    boots = [sums[s].sum() / cnts[s].sum() for s in (rng.integers(0, len(uniq), len(uniq)) for _ in range(2000))]
    f1 = {"n": len(d), "nll_market": float(np.mean(nll_raw)), "nll_adjusted": float(np.mean(nll_adj)),
          "improve": float(d.mean()), "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}
    f2 = {f"{a}|{b}": {"bets": n, "roi": round(s / n, 4)} for (a, b), (s, n) in roi.items()}

    def pool_roi(kind, period, tau):
        ret = bets = 0
        rets = defaultdict(lambda: [0.0, 0])
        for rid, r in races.items():
            lo, hi = DISCOVERY if period == "discovery" else CONFIRM
            if not (lo <= rid[:8] <= hi) or rid not in truth:
                continue
            od = r.get("odds") or {}
            if not od.get("official_dt"):
                continue
            keys, po, pt = pair_probs(od, kind)
            lst = od.get("list_6") if kind == "exacta" else od.get("list_5")
            oddmap = {}
            for x in lst or []:
                c = (str(int(x[0][:2])), str(int(x[0][2:4])))
                c = c if kind == "exacta" else tuple(sorted(c, key=int))
                if float(x[1] or 0) > 0:
                    oddmap[c] = float(x[1])
            t = truth[rid]
            tgt = (t[0], t[1]) if kind == "exacta" else tuple(sorted((t[0], t[1]), key=int))
            for k, a_, b_ in zip(keys, po, pt):
                if b_ / a_ > tau and k in oddmap:
                    win = oddmap[k] if k == tgt else 0.0
                    ret += win
                    bets += 1
                    rets[rid[:8]][0] += win
                    rets[rid[:8]][1] += 1
        if not bets:
            return None
        dd = sorted(rets)
        vec = np.array([rets[x] for x in dd])
        bs = []
        for _ in range(2000):
            s = vec[rng.integers(0, len(dd), len(dd))].sum(0)
            bs.append(s[0] / s[1])
        return {"tau": tau, "bets": bets, "roi": ret / bets, "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}

    f3 = {}
    for kind in ("exacta", "quinella"):
        best = None
        for tau in (1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0):
            rr = pool_roi(kind, "discovery", tau)
            if rr and rr["bets"] >= 2000 and (best is None or rr["roi"] > best["roi"]):
                best = rr
        f3[kind] = {"discovery_best": best, "confirm": pool_roi(kind, "confirm", best["tau"]) if best else None}
    return {"F1_structural_recalibration": f1, "F2_roi_by_structure_confirm": f2, "F3_cross_pool_roi": f3}


def main_followup(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", nargs="+", required=True)
    ap.add_argument("--kdata-races", required=True)
    ap.add_argument("--scan", required=True, help="main の出力（S1の発見期間の比を使う）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    races, truth = load(a.hist, a.kdata_races)
    races = {k: v for k, v in races.items() if DISCOVERY[0] <= k[:8] <= CONFIRM[1]}
    res = followup(races, truth, json.load(open(a.scan))["s1"])
    json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))
