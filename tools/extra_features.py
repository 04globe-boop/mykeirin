"""Kドリームス・オッズパークの収集データ（extra_YYYYMM.jsonl.gz）から、選手ごとの行を作る。

出力（gzip CSV、1選手1行）：
  race_id, car, name
  k_mark, k_kiai, k_sohyo     Kドリームスの記者の予想印・好気合・総評
  k_comment                   Kドリームスの選手コメント（レース前）
  k_prev_hyo / k_tanpyo       前回の走り評 / 初日の短評（記者のひとこと評価）
  op_comment                  オッズパークの選手コメント（レース前。Kドリームスとは別の取材文）
  after_quote                 このレースの「戦い終わって」で、この選手が話した部分（レース後。次走の予測に使う）
  result_rank, result_hyo     このレースの着順と勝敗因（Kドリームスの結果表）
  （各談話について danwa_dict.tag の結果：k_*, op_*, af_* の pos/neg/equip/train/hurt）
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from danwa_dict import tag  # noqa: E402

NAME_RE = re.compile(r"^(.+?)\s+(\S\u3000\S|\S{3})/\d+/\d+$")  # 例「寺沼 拓摩 東　京/27/115」「菅原 大也 神奈川/35/107」
SCORE_RE = re.compile(r"^\d{2,3}\.\d{1,2}$")


def parse_k_racecard(rows: list[list[str]]) -> dict[str, dict]:
    out = {}
    for cells in rows:
        i_name = next((i for i, c in enumerate(cells) if NAME_RE.match(c)), None)
        if i_name is None or i_name < 4 or not cells[i_name - 1].isdigit():
            continue
        car = str(int(cells[i_name - 1]))
        name = NAME_RE.match(cells[i_name]).group(1).replace(" ", "").replace("　", "")
        i_score = next((i for i in range(i_name + 1, len(cells)) if SCORE_RE.match(cells[i])), None)
        rest = cells[i_score + 1:] if i_score is not None else []
        rec = {"name": name, "k_mark": cells[0], "k_kiai": cells[1], "k_sohyo": cells[2],
               "k_comment": rest[0] if rest else "", "k_prev_hyo": "", "k_tanpyo": ""}
        if len(rest) >= 5:
            rec["k_prev_hyo"] = re.sub(r"\s*詳細$", "", rest[4])
        elif len(rest) == 2:
            rec["k_tanpyo"] = rest[1]
        out[car] = rec
    return out


def parse_k_result(rows) -> dict[str, dict]:
    out = {}
    for cells in (rows or [])[1:]:
        if len(cells) >= 9 and cells[2].isdigit():
            out[str(int(cells[2]))] = {"result_rank": cells[1], "result_hyo": cells[8]}
    return out


def parse_op(rows) -> dict[str, str]:
    out = {}
    for cells in (rows or [])[1:]:
        if len(cells) >= 3 and cells[-3].isdigit():
            out[str(int(cells[-3]))] = cells[-1]
    return out


QUOTE_RE = re.compile(r"([^\s「」。、（）()]{1,8})は「(.+?)」")


def after_quotes(text: str, names: dict[str, str]) -> dict[str, str]:
    """「戦い終わって」から、選手ごとの発言を取り出す（「佐藤は「…」」の形。姓で照合）。"""
    out: dict[str, list[str]] = {}
    for who, q in QUOTE_RE.findall(text or ""):
        for car, name in names.items():
            if who and (name.startswith(who) or who.startswith(name[:2])) and len(who) >= 2:
                out.setdefault(car, []).append(q)
                break
    return {k: " / ".join(v) for k, v in out.items()}


COLS = ["race_id", "car", "name", "k_mark", "k_kiai", "k_sohyo", "k_comment", "k_prev_hyo", "k_tanpyo",
        "op_comment", "after_quote", "result_rank", "result_hyo"]
TAGS = ["pos", "neg", "equip", "train", "hurt", "len"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("extra_glob")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    k, op = {}, {}
    for p in sorted(glob.glob(a.extra_glob)):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "k":
                    k[r["race_id"]] = r["data"]
                elif r.get("type") == "op":
                    op[r["race_id"]] = r["data"]
    cols = COLS + [f"{p}_{t}" for p in ("k", "op", "af") for t in TAGS]
    n = 0
    with gzip.open(a.out, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for rid in sorted(set(k) | set(op)):
            kd = k.get(rid) or {}
            card = {}
            for t in kd.get("racecard") or []:
                if t and any("選手コメント" in c for c in t[0]):  # 選手コメントと前回の走り評／短評の表
                    card = parse_k_racecard(t)
                    break
            res = parse_k_result(kd.get("result"))
            opc = parse_op((op.get(rid) or {}).get("comments"))
            names = {c: v["name"] for c, v in card.items()}
            aq = after_quotes(kd.get("after") or "", names)
            for car in sorted(set(card) | set(opc), key=int):
                row = {"race_id": rid, "car": car, **{c: "" for c in COLS[2:]}}
                row.update(card.get(car, {}))
                row.update(res.get(car, {}))
                row["op_comment"] = opc.get(car, "")
                row["after_quote"] = aq.get(car, "")
                for pfx, txt in (("k", row["k_comment"]), ("op", row["op_comment"]), ("af", row["after_quote"])):
                    for t, v in tag(txt).items():
                        row[f"{pfx}_{t}"] = v
                w.writerow(row)
                n += 1
    print(json.dumps({"k_races": len(k), "op_races": len(op), "rows": n}))


if __name__ == "__main__":
    main()
