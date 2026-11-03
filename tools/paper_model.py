"""買ったつもり帳用：Codex側の前向き予測（forward-YYYY-MM の Releases）を読み、車番つきの3連単確率にする。読むだけ。

使うのは拡充版 v2 の GBDT+GNN 融合（Codex側の主候補。オッズを入力に使わない独自モデル）。
  model_id = 2ad4a38e5ee7c6ec1075aaef88fa72af7e0031ee13e8016c8c56781d1f475913
  （mykeirin2/reports/FORWARD_BUNDLE_V2_RECEIPT.json の candidate_ids["gbdt+gnn"]。凍結は 2026-10-20）

予測は締切5分前の約20秒手前に Codex側の仕組みが記録したもの（後から作り直したものではない）。
選手は合成ID（composite:…）で入っているので、forward_inputs の rider_ids（欠車を除いた車番順。keirin_lab/live_data.py）
を、ロガーの締切5分前オッズで売れている車番に順に対応させる。人数が合わないレースは使わない。
"""
from __future__ import annotations

import gzip
import json
import sqlite3

MODEL_ID = "2ad4a38e5ee7c6ec1075aaef88fa72af7e0031ee13e8016c8c56781d1f475913"
MODEL_NAME = "Codex v2 GBDT+GNN"


def active_cars(od: dict) -> list[str]:
    """締切5分前の3連単オッズで売れている車番（欠車を除く）。"""
    cars = set()
    for x in od.get("list_9") or []:
        try:
            if float(x[1]) > 0:
                cars.update(str(int(x[0][i:i + 2])) for i in (0, 2, 4))
        except (TypeError, ValueError):
            continue
    return sorted(cars, key=int)


def load_day(sqlite_path: str, inputs_path: str, model_id: str = MODEL_ID) -> dict:
    """race_id -> {"riders": [...車番順の合成ID], "p": {合成IDの3つ組: 確率}, "recorded_at", "close_at", "eligible"}"""
    riders = {}
    with gzip.open(inputs_path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("synthetic"):
                continue
            ids = tuple(r.get("rider_ids") or [])
            prev = riders.get(r["race_id"])
            # 同じレースの入力が複数あるとき、選手の並びが違えば使わない（対応が決められないため）
            riders[r["race_id"]] = ids if prev in (None, ids) else None
    out = {}
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        q = "select race_id, payload, recorded_at, close_at from predictions where model_id=? and synthetic=0"
        for rid, payload, rec, close in con.execute(q, (model_id,)):
            if rec > close:  # 締切後の記録は使わない（仕組み上ないはずだが念のため）
                continue
            pl = json.loads(payload)
            if not pl.get("outcomes") or not pl.get("p"):
                continue
            out[rid] = {"riders": riders.get(rid), "p": {tuple(o): float(v) for o, v in zip(pl["outcomes"], pl["p"])},
                        "recorded_at": rec, "close_at": close, "eligible": bool(pl.get("primary_horizon_eligible"))}
    finally:
        con.close()
    return out


def to_cars(pred: dict | None, od: dict) -> tuple[dict | None, str]:
    """車番の3つ組 -> 確率。対応できなければ (None, 理由)。"""
    if not pred:
        return None, "予測なし"
    if not pred.get("riders"):
        return None, "選手の並び不明"
    cars = active_cars(od)
    if len(cars) != len(pred["riders"]):
        return None, "人数不一致"
    car_of = dict(zip(pred["riders"], cars))
    p = {}
    for o, v in pred["p"].items():
        if not all(x in car_of for x in o):
            return None, "選手の対応不一致"
        p[tuple(car_of[x] for x in o)] = v
    z = sum(p.values())
    if not z > 0:
        return None, "確率なし"
    return {c: v / z for c, v in p.items()}, "ok"
