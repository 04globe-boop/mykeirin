"""netkeirin の内部API（https://keirin.netkeiba.com/api/race/）クライアント。

ページのJS（race_api_indexdb.min.js の AplApiRace）が使っている呼び出し方をそのまま再現する。
POST、フォーム形式、パラメータ: input=UTF-8, output=json, class=<クラス名>, method=get, compress=0, <クラスごとの引数>

確認済みのクラス（2026-10-19 にブラウザから実通信で確認）:
  AplRace        引数 kaisai_date(YYYYMMDD), syusai   … その日の全場・全レース（発走・締切時刻、車立て、状態）
  AplRaceOdds    引数 race_id(12桁)                    … 全券種のオッズ（list_3〜list_9）と official_dt
  AplNarabiYoso2 引数 race_id                          … 並び予想（syaban "0" がラインの区切り）と本紙の印
  AplRaceHorse   引数 race_id                          … 出走表（車番、枠番、氏名、級班、府県、期別、年齢、得点）

券種の対応（AplRaceOdds）:
  list_3=2枠複, list_4=2枠単, list_5=2車複, list_6=2車単, list_7=ワイド(下限,上限), list_8=3連複, list_9=3連単
  各要素は [組番, オッズ, ワイド上限または"", 人気順]
"""
from __future__ import annotations

import json
import random
import time

import requests

API_URL = "https://keirin.netkeiba.com/api/race/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Referer": "https://keirin.netkeiba.com/",
    "Origin": "https://keirin.netkeiba.com",
    "X-Requested-With": "XMLHttpRequest",
}
BET_TYPES = {
    "list_3": "2枠複",
    "list_4": "2枠単",
    "list_5": "2車複",
    "list_6": "2車単",
    "list_7": "ワイド",
    "list_8": "3連複",
    "list_9": "3連単",
}


class ApiError(RuntimeError):
    pass


class NetkeirinClient:
    def __init__(self, min_interval: float = 0.6, timeout: float = 20.0, retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap + random.uniform(0, 0.2))
        self._last = time.monotonic()

    def call(self, cls: str, **params) -> dict:
        """API を呼び、data 部分（キー→値）を返す。_last_dt で終わるキーは除く。"""
        body = {"input": "UTF-8", "output": "json", "class": cls, "method": "get", "compress": "0"}
        body.update({k: str(v) for k, v in params.items()})
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            try:
                r = self.session.post(API_URL, data=body, timeout=self.timeout)
                r.raise_for_status()
                j = r.json()
                if j.get("status") == "NG":
                    raise ApiError(f"{cls} {params}: status=NG")
                data = j.get("data") or {}
                out = {}
                for k, v in data.items():
                    if k.endswith("_last_dt"):
                        continue
                    if isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass
                    out[k] = v
                return out
            except (requests.RequestException, ValueError, ApiError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise ApiError(f"{cls} {params}: {last_err}")

    @staticmethod
    def _single(data: dict):
        if len(data) != 1:
            # 想定外の形。呼び出し側で生データごと保存する
            return data
        return next(iter(data.values()))

    def race_list(self, kaisai_date: str) -> list[dict]:
        """その日の全レース（場ごとの list を平らにする）。"""
        v = self._single(self.call("AplRace", kaisai_date=kaisai_date, syusai="1"))
        races: list[dict] = []
        if isinstance(v, list):
            for venue in v:
                for race in venue.get("list", []):
                    races.append(race)
        return races

    def odds(self, race_id: str) -> dict:
        return self._single(self.call("AplRaceOdds", race_id=race_id))

    def lines(self, race_id: str) -> dict:
        return self._single(self.call("AplNarabiYoso2", race_id=race_id))

    def entries(self, race_id: str):
        return self._single(self.call("AplRaceHorse", race_id=race_id))


def parse_lines(narabi: dict) -> list[list[str]]:
    """AplNarabiYoso2 の lineForecast を [[車番,...], ...] のライン列に変換する。

    syaban "0" はラインの区切り。区切りが連続する場合や先頭・末尾の区切りは無視する。
    """
    out: list[list[str]] = []
    for seq in (narabi or {}).get("lineForecast", []) or []:
        cur: list[str] = []
        for x in seq:
            s = str(x.get("syaban", "0"))
            if s == "0":
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append(s)
        if cur:
            out.append(cur)
    return out
