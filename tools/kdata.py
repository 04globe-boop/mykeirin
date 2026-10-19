"""公開データ（Kenseimk/keirin-data、Kドリームス由来）の正規化読み込みとIDの統一。

既知の問題と対処：
1. 年によって列の並びが違う（2022/01-03, 2022/04-2025/12, 2026/01-03, 2026/04, 2026/05-）
   → ファイルごとにヘッダーから列位置を引く（行を後から共通の列番号で読まない）
2. date 列は「開催初日」で、実際のレース日ではない
   → KドリームスのレースID（場2桁＋開催初日8桁＋日目2桁＋00＋R2桁）から
     実際のレース日 = 開催初日 + (日目-1)日 を計算する（順延などの例外は netkeirin の一覧で後から照合）
3. 府県が名前の欄に食い込む行がある（例：名前「伊早坂 駿一  茨」、府県「城」）
   → 名前の末尾の2つの空白以降を府県の先頭に戻す

出力（gzip CSV）：
  runners.csv.gz  1選手1行（nk_race_id, k_race_id, race_date, venue_code, race_no, 車番, 選手, 級班, 得点, 着順 …）
  races.csv.gz    1レース1行（払戻・人気・車立て・品質フラグ）
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

PREFS = ["北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島", "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
         "新潟", "富山", "石川", "福井", "山梨", "長野", "岐阜", "静岡", "愛知", "三重", "滋賀", "京都", "大阪", "兵庫",
         "奈良", "和歌山", "鳥取", "島根", "岡山", "広島", "山口", "徳島", "香川", "愛媛", "高知", "福岡", "佐賀", "長崎",
         "熊本", "大分", "宮崎", "鹿児島", "沖縄"]
PAY_RE = re.compile(r"^\s*([\d=\-]+)\s+([\d,]+)円\((\d+)\)\s*$")


def split_name_pref(name: str, pref: str) -> tuple[str, str]:
    name = (name or "").strip()
    pref = (pref or "").strip()
    if "  " in name:
        n, head = name.rsplit("  ", 1)
        cand = head.strip() + pref
        if cand in PREFS or pref == "":
            return n.strip(), cand
    return name, pref


def parse_k_id(kid: str):
    """Kドリームスのレースid → (場コード, 開催初日, 日目, R)。形式外なら None。"""
    if not re.fullmatch(r"\d{16}", kid or ""):
        return None
    try:
        first = datetime.strptime(kid[2:10], "%Y%m%d")
    except ValueError:
        return None
    return kid[:2], first, int(kid[10:12]), int(kid[14:16])


def parse_pay(s: str):
    m = PAY_RE.match((s or "").replace("　", " "))
    if not m:
        return None, None, None
    return m.group(1), int(m.group(2).replace(",", "")), int(m.group(3))


RUNNER_COLS = ["nk_race_id", "k_race_id", "race_date", "kaisai_first", "nichime", "venue_code", "venue_slug", "race_no",
               "car_no", "player_name", "pref", "age", "term", "player_class", "running_style", "gear", "race_score",
               "rank", "agari", "finish_type", "margin", "win_lose", "mark", "win_rate_4m", "top2_rate_4m",
               "top3_rate_4m", "nige_4m", "maku_4m", "source_file"]
RACE_COLS = ["nk_race_id", "k_race_id", "race_date", "kaisai_first", "nichime", "venue_code", "venue_slug", "race_no",
             "n_runners", "is_s_class_all", "lineup_marks", "close_time",
             "ex_combo", "ex_pay", "ex_pop", "tri_combo", "tri_pay", "tri_pop", "trio_combo", "trio_pay", "trio_pop",
             "n_rank1", "flags"]


def load(data_dir: str):
    races: dict[str, dict] = {}
    runners: list[dict] = []
    issues = Counter()
    for fn in sorted(glob.glob(os.path.join(data_dir, "*_keirin.csv"))):
        with open(fn, encoding="utf-8-sig", errors="replace") as f:
            r = csv.reader(f)
            h = next(r)
            ix = {k: i for i, k in enumerate(h)}

            def g(row, k):
                i = ix.get(k)
                return row[i] if i is not None and i < len(row) else ""

            for row in r:
                if len(row) != len(h):
                    issues["row_length_mismatch"] += 1
                    continue
                kid = g(row, "race_id")
                p = parse_k_id(kid)
                if p is None:
                    issues["bad_k_race_id"] += 1
                    continue
                vc, first, nichime, rno = p
                rdate = first + timedelta(days=nichime - 1)
                nk = rdate.strftime("%Y%m%d") + vc + f"{rno:02d}"
                name, pref = split_name_pref(g(row, "player_name"), g(row, "pref"))
                if pref and pref not in PREFS:
                    issues["pref_unrecognized"] += 1
                rr = {"nk_race_id": nk, "k_race_id": kid, "race_date": rdate.strftime("%Y-%m-%d"),
                      "kaisai_first": first.strftime("%Y-%m-%d"), "nichime": nichime, "venue_code": vc,
                      "venue_slug": g(row, "venue_slug"), "race_no": rno, "car_no": g(row, "banum"),
                      "player_name": name, "pref": pref, "source_file": os.path.basename(fn)}
                for k in ("age", "term", "player_class", "running_style", "gear", "race_score", "rank", "agari",
                          "finish_type", "margin", "win_lose", "mark", "win_rate_4m", "top2_rate_4m", "top3_rate_4m",
                          "nige_4m", "maku_4m"):
                    v = g(row, k)
                    rr[k] = "" if v in ("nan", "None") else v
                runners.append(rr)
                if nk not in races:
                    ex = parse_pay(g(row, "ni_sha_tan"))
                    tri = parse_pay(g(row, "san_ren_tan"))
                    trio = parse_pay(g(row, "san_ren_fuku"))
                    races[nk] = {"nk_race_id": nk, "k_race_id": kid, "race_date": rr["race_date"],
                                 "kaisai_first": rr["kaisai_first"], "nichime": nichime, "venue_code": vc,
                                 "venue_slug": rr["venue_slug"], "race_no": rno, "lineup_marks": g(row, "lineup"),
                                 "close_time": g(row, "close_time"),
                                 "ex_combo": ex[0], "ex_pay": ex[1], "ex_pop": ex[2],
                                 "tri_combo": tri[0], "tri_pay": tri[1], "tri_pop": tri[2],
                                 "trio_combo": trio[0], "trio_pay": trio[1], "trio_pop": trio[2]}
    # レース単位の集計と品質フラグ
    by_race = defaultdict(list)
    for rr in runners:
        by_race[rr["nk_race_id"]].append(rr)
    for nk, rs in by_race.items():
        R = races[nk]
        cars = [x["car_no"] for x in rs]
        flags = []
        if len(cars) != len(set(cars)):
            flags.append("dup_car")
            issues["race_dup_car"] += 1
        R["n_runners"] = len(set(cars))
        R["is_s_class_all"] = int(all(x["player_class"].startswith("S") for x in rs))
        n1 = sum(1 for x in rs if x["rank"] == "1")
        R["n_rank1"] = n1
        if n1 != 1:
            flags.append("rank1_count_" + str(n1))
            issues["race_rank1_not_1"] += 1
        if R["tri_pay"] is None:
            flags.append("no_trifecta_payout")
            issues["race_no_trifecta_payout"] += 1
        elif R["tri_combo"]:
            top3 = sorted((x for x in rs if x["rank"].isdigit()), key=lambda x: int(x["rank"]))[:3]
            if "-".join(x["car_no"] for x in top3) != R["tri_combo"]:
                flags.append("trifecta_mismatch_ranks")
                issues["race_trifecta_mismatch_ranks"] += 1
        R["flags"] = ";".join(flags)
    return races, runners, issues


def write_csv(path, cols, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args(argv)
    races, runners, issues = load(a.data_dir)
    os.makedirs(a.out_dir, exist_ok=True)
    write_csv(os.path.join(a.out_dir, "runners.csv.gz"), RUNNER_COLS, runners)
    write_csv(os.path.join(a.out_dir, "races.csv.gz"), RACE_COLS, sorted(races.values(), key=lambda r: r["nk_race_id"]))
    years = Counter(r["race_date"][:4] for r in races.values())
    s_years = Counter(r["race_date"][:4] for r in races.values() if r.get("is_s_class_all"))
    report = {"races": len(races), "runners": len(runners), "issues": dict(issues),
              "races_by_year": dict(sorted(years.items())), "s_class_races_by_year": dict(sorted(s_years.items())),
              "flagged_races": sum(1 for r in races.values() if r.get("flags"))}
    with open(os.path.join(a.out_dir, "qa_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
