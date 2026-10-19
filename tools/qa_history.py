"""過去データ（hist_YYYYMM.jsonl.gz）の品質チェックと、公開データ（kdata の races.csv.gz）とのID照合。

チェック項目（レースごと）：
- 全組み合わせのオッズがそろっているか（車立てから理論上の組数を計算して比較）
- 確定オッズか（official_dt が入っているか）
- 各券種の控除後の払戻率（1/Σ(1/オッズ)）が 0.70〜0.78 に収まるか
- 並び予想に全車が1回ずつ入っているか
- 出走表の人数が車立てと一致するか
ID照合：
- netkeirin の race_id と、公開データから計算した race_id の一致
- netkeirin の開催初日・日目から作った KドリームスID と、公開データの KドリームスID の一致（順延などの検出）
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from math import comb, perm

EXPECTED = {"list_5": lambda n: comb(n, 2), "list_6": lambda n: perm(n, 2), "list_7": lambda n: comb(n, 2),
            "list_8": lambda n: comb(n, 3), "list_9": lambda n: perm(n, 3)}
NAMES = {"list_5": "2車複", "list_6": "2車単", "list_7": "ワイド", "list_8": "3連複", "list_9": "3連単"}


def payout_rate(lst):
    inv = 0.0
    for x in lst:
        try:
            o = float(x[1])
        except (ValueError, TypeError, IndexError):
            continue
        if o > 0:
            inv += 1 / o
    return 1 / inv if inv else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("hist", nargs="+")
    ap.add_argument("--kdata-races", help="kdata.py が出力した races.csv.gz")
    a = ap.parse_args(argv)

    races, lists = {}, {}
    for p in a.hist:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "race":
                    races[r["race_id"]] = r
                elif r.get("type") == "racelist":
                    for x in r.get("data") or []:
                        lists[x["race_id"]] = x

    c = Counter()
    rate_out = Counter()
    examples = {}
    for rid, r in races.items():
        meta = lists.get(rid, {})
        try:
            n = int(meta.get("tosu") or 0)
        except ValueError:
            n = 0
        od = r.get("odds") or {}
        c["races"] += 1
        if r.get("errors"):
            c["fetch_errors"] += 1
        if not od.get("official_dt"):
            c["no_official_dt"] += 1
            examples.setdefault("no_official_dt", rid)
        # 欠車：2車単で、その車番が絡む目のオッズがすべて0
        cars = {x[0][:2] for x in od.get("list_6") or []} | {x[0][2:4] for x in od.get("list_6") or []}
        scratched = {car for car in cars
                     if all(float(x[1] or 0) == 0 for x in od.get("list_6") or [] if car in (x[0][:2], x[0][2:4]))}
        if scratched:
            c["races_with_scratch"] += 1
        c["trifecta_capped_9999"] += sum(1 for x in od.get("list_9") or [] if float(x[1] or 0) >= 9999.9)
        c["trifecta_combos"] += len(od.get("list_9") or [])
        for k, fn in EXPECTED.items():
            lst = od.get(k) or []
            if n and len(lst) != fn(n):
                c[f"count_mismatch_{NAMES[k]}"] += 1
                examples.setdefault(f"count_mismatch_{NAMES[k]}", f"{rid} n={n} got={len(lst)} exp={fn(n)}")
            if k == "list_7":
                continue  # ワイドは1レースで3組が的中するので、1/Σ(1/オッズ) は払戻率にならない
            pr = payout_rate(lst)
            if pr is not None and not (0.70 <= pr <= 0.78):
                rate_out[NAMES[k]] += 1
                examples.setdefault(f"rate_{NAMES[k]}", f"{rid} n={n} {pr:.3f}")
        ent = r.get("entries") or []
        if n and isinstance(ent, list) and len(ent) != n:
            c["entries_count_mismatch"] += 1
            examples.setdefault("entries_count_mismatch", f"{rid} n={n} entries={len(ent)}")
        lp = r.get("lines_parsed") or []
        flat = [x for line in lp for x in line]
        # 欠車の選手は、並び予想に残っている場合（欠車の発表前に作られた予想）と、消えている場合がある。
        # 欠車を除いた並びが、走る選手全員をちょうど1回ずつ含むかを見る
        n_run = n - len(scratched)
        if any(f"{int(x):02d}" in scratched for x in flat if str(x).isdigit()):
            c["lines_include_scratched"] += 1
        flat = [x for x in flat if not (str(x).isdigit() and f"{int(x):02d}" in scratched)]
        if not lp:
            c["lines_missing"] += 1
        elif n and (len(flat) != n_run or len(set(flat)) != n_run):
            c["lines_incomplete"] += 1
            examples.setdefault("lines_incomplete", f"{rid} n={n} lines={lp}")
    report = {"counts": dict(c), "payout_rate_out_of_range": dict(rate_out), "examples": examples,
              "days": len({rid[:8] for rid in races}), "racelist_races": len(lists)}

    if a.kdata_races:
        kd = {}
        with gzip.open(a.kdata_races, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                kd[row["nk_race_id"]] = row
        days = {rid[:8] for rid in races}
        kd_month = {k: v for k, v in kd.items() if k[:8] in days}
        both = set(races) & set(kd_month)
        k_mismatch = []
        for rid in both:
            m = lists.get(rid, {})
            k_from_nk = f"{rid[8:10]}{m.get('kaisai_date_1st', '')}{int(m.get('nichiji') or 0):02d}00{rid[10:12]}"
            if k_from_nk != kd_month[rid]["k_race_id"]:
                k_mismatch.append((rid, k_from_nk, kd_month[rid]["k_race_id"]))
        report["id_crosswalk"] = {"netkeirin_races": len(races), "kdata_races_same_days": len(kd_month),
                                  "matched": len(both), "only_netkeirin": len(set(races) - set(kd_month)),
                                  "only_kdata": len(set(kd_month) - set(races)),
                                  "k_id_mismatch": len(k_mismatch), "k_id_mismatch_examples": k_mismatch[:5]}
        # 確定オッズと公開データの払戻の照合（3連単）
        pay_ok = pay_ng = 0
        ng_ex = []
        for rid in both:
            row = kd_month[rid]
            if not row.get("tri_combo") or not row.get("tri_pay"):
                continue
            combo = "".join(f"{int(x):02d}" for x in row["tri_combo"].split("-"))
            lst = {x[0]: x[1] for x in (races[rid].get("odds") or {}).get("list_9") or []}
            if combo in lst:
                try:
                    if abs(float(lst[combo]) * 100 - int(row["tri_pay"])) <= 10:
                        pay_ok += 1
                    else:
                        pay_ng += 1
                        ng_ex.append((rid, row["tri_combo"], lst[combo], row["tri_pay"]))
                except ValueError:
                    pay_ng += 1
        report["trifecta_payout_check"] = {"match": pay_ok, "mismatch": pay_ng, "examples": ng_ex[:5]}
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
