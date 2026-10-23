"""締切前オッズの記録プログラム（1回の起動で window 分だけ動き、締切前の決まった時点でオッズを取る）。

GitHub Actions から10分おきに起動する前提。起動が遅れても、取り逃した時点は許容範囲内なら取り直し、
範囲外なら「取れなかった」と記録する（推測で埋めない）。

保存形式：data/odds_YYYYMMDD.jsonl.gz（開催日ごと、1行1レコード、gzip の追記）
  type=racelist  その日の全レース一覧（内容が変わったときだけ保存。締切の繰り下げなどを追跡）
  type=odds      全券種のオッズ（label は締切からの分。小数の追加枠も保存し、実取得完了で時点判定）
  type=lines     並び予想（ライン区切り付き）。-60/-7/-5で取得し、直前の変更を追跡
  type=entries   出走表。-60/-7/-5で取得し、欠車などの変更を追跡
  type=miss      予定した時点を取り逃したことの記録
  type=error     通信エラーなど
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netkeirin import NetkeirinClient, parse_lines  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
PRE_OFFSETS = [-60, -30, -20, -15, -10, -7, -5.5, -5, -3, -2.5, -2, -1.5, -1]  # 締切からの分
PRE_CUTOFF_HORIZONS = {-5.5: 5, -2.5: 2, -1.5: 1}
PRE_CUTOFF_GRACE_SECONDS = 6.0  # 取得はtarget+6秒まで。T-5の実発行開始まで4秒を残す。
PRE_CUTOFF_BUDGET_SECONDS = 5.0
CAPTURE_SCHEDULE_VERSION = 'precutoff-30s-v1'
POST_OFFSETS = [8, 15, 30]  # 確定値（official_dt あり）が取れた時点で以降は取らない
CARD_OFFSETS = {-60, -7, -5}  # -7 also supplies an actually captured card before the T-5 forecast
RACELIST_REFRESH_SEC = 300
POST_DEADLINE_MIN = 180  # 締切後この分数までは確定オッズを取りに行く
MISS_RECORD_WINDOW_MIN = 30  # これより古い予定時刻の取り逃しはファイルに書かない（初回起動時のノイズ防止）
RACELIST_FIELDS = ("race_id", "jyo_cd", "jyo", "race_name", "jyoken", "kaisai_date", "kaisai_date_1st", "nichiji",
                   "tosu", "kyori", "start", "close", "race_status", "sale_status", "tenko", "update_datetime")
RACELIST_HASH_FIELDS = RACELIST_FIELDS


def tolerance_min(offset: float) -> float:
    """予定時刻を過ぎてから取り直してよい時間（分）。締切に近い時点ほど狭くする。"""
    if offset in PRE_CUTOFF_HORIZONS:
        return PRE_CUTOFF_GRACE_SECONDS / 60
    a = abs(offset)
    if a <= 3:
        return 0.75
    if a <= 10:
        return 2.0
    return 5.0


def label_of(offset: float) -> str:
    return f"{offset:+g}"


@dataclass
class Race:
    race_id: str
    kaisai_date: str
    close: datetime
    start: datetime
    raw: dict


@dataclass
class State:
    captured: set = field(default_factory=set)  # (race_id, type, label)
    finals: set = field(default_factory=set)  # 確定オッズ（official_dt あり）を取得済みのレース
    racelist_hash: dict = field(default_factory=dict)  # kaisai_date -> hash


def parse_hhmm(day: str, hhmm: str) -> datetime | None:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    base = datetime.strptime(day, "%Y%m%d").replace(tzinfo=JST)
    return base + timedelta(hours=h, minutes=m)


def to_races(raw_list: list[dict]) -> list[Race]:
    out = []
    for r in raw_list:
        day = r.get("kaisai_date")
        close = parse_hhmm(day, r.get("close", ""))
        start = parse_hhmm(day, r.get("start", ""))
        if not r.get("race_id") or close is None:
            continue
        out.append(Race(r["race_id"], day, close, start or close, r))
    return out


def day_path(out_dir: str, day: str) -> str:
    return os.path.join(out_dir, f"odds_{day}.jsonl.gz")


def load_state(out_dir: str, days: list[str]) -> State:
    st = State()
    for d in days:
        p = day_path(out_dir, d)
        if not os.path.exists(p):
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("type")
                if t in ("odds", "lines", "entries", "miss"):
                    typ = "odds" if t == "miss" else t
                    st.captured.add((rec.get("race_id"), typ, rec.get("label")))
                    if t == "odds" and str(rec.get("label", "")).startswith("+") and (rec.get("data") or {}).get("official_dt"):
                        st.finals.add(rec.get("race_id"))
                elif t == "racelist":
                    st.racelist_hash[rec.get("kaisai_date")] = rec.get("hash")
    return st


def append(out_dir: str, day: str, rec: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with gzip.open(day_path(out_dir, day), "at", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def iso(dt: datetime) -> str:
    return dt.astimezone(JST).isoformat(timespec="seconds")


class Logger:
    def __init__(self, client, out_dir: str, window_min: float, clock=None, sleeper=None, log=print,forecaster=None,pre_capture=None):
        self.client = client
        self.out_dir = out_dir
        self.window = timedelta(minutes=window_min)
        self.clock = clock or (lambda: datetime.now(JST))
        self.sleep = sleeper or time.sleep
        self.log = log
        self.forecaster=forecaster
        self.pre_capture=pre_capture
        self.races: dict[str, Race] = {}
        self.last_refresh: datetime | None = None
        self.counts = {"odds": 0, "lines": 0, "entries": 0, "miss": 0, "error": 0}

    def days_to_watch(self, now: datetime) -> list[str]:
        days = [now.strftime("%Y%m%d")]
        if now.hour < 3:  # 深夜帯の起動では前日分も見る
            days.insert(0, (now - timedelta(days=1)).strftime("%Y%m%d"))
        return days

    def refresh(self, now: datetime) -> None:
        for d in self.days_to_watch(now):
            try:
                raw = self.client.race_list(d)
            except Exception as e:  # noqa: BLE001
                self.record_error(d, None, "racelist", e, now)
                continue
            compact = sorted(({k: r.get(k) for k in RACELIST_FIELDS} for r in raw), key=lambda r: r.get("race_id") or "")
            key = [[r.get(k) for k in RACELIST_HASH_FIELDS] for r in compact]
            h = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:16]
            if self.state.racelist_hash.get(d) != h:
                append(self.out_dir, d, {"type": "racelist", "kaisai_date": d,
                       "fetched_at": self.clock().astimezone(JST).isoformat(timespec="microseconds"), "hash": h, "data": compact})
                self.state.racelist_hash[d] = h
            for r in to_races(raw):
                self.races[r.race_id] = r
        self.last_refresh = now

    def record_error(self, day, race_id, what, err, now):
        self.counts["error"] += 1
        append(self.out_dir, day, {"type": "error", "race_id": race_id, "what": what, "at": iso(now), "error": str(err)[:500]})
        self.log(f"[error] {what} {race_id}: {err}")

    def events(self, now: datetime, end: datetime):
        """(予定時刻, race, offset) のうち、この起動で扱うものを返す。"""
        ev = []
        for r in self.races.values():
            for off in PRE_OFFSETS:
                if (r.race_id, "odds", label_of(off)) in self.state.captured:
                    continue
                t = r.close + timedelta(minutes=off)
                if t > end:
                    continue
                ev.append((t, r, off))
            for off in POST_OFFSETS:
                if r.race_id in self.state.finals:
                    break
                if (r.race_id, "odds", label_of(off)) in self.state.captured:
                    continue
                t = r.close + timedelta(minutes=off)
                if t <= end and now <= r.close + timedelta(minutes=POST_DEADLINE_MIN):
                    ev.append((t, r, off))
        ev.sort(key=lambda x: x[0])
        return ev

    def fetch(self, r: Race, off: float, target: datetime, now: datetime) -> None:
        lab = label_of(off)
        base = {"race_id": r.race_id, "label": lab, "target_at": iso(target),
                "close": r.raw.get("close"), "start": r.raw.get("start"),
                "race_status": r.raw.get("race_status"), "sale_status": r.raw.get("sale_status"),
                "capture_schedule_version": CAPTURE_SCHEDULE_VERSION}
        if off in PRE_CUTOFF_HORIZONS:
            self.fetch_pre_cutoff(r, off, target, base)
            return
        try:
            od = self.client.odds(r.race_id)
            got = self.clock()
            append(self.out_dir, r.kaisai_date, {"type": "odds", **base,
                   "fetched_at": got.astimezone(JST).isoformat(timespec="microseconds"), "data": od})
            self.counts["odds"] += 1
            if off > 0 and isinstance(od, dict) and od.get("official_dt"):
                self.state.finals.add(r.race_id)
        except Exception as e:  # noqa: BLE001
            self.record_error(r.kaisai_date, r.race_id, f"odds{lab}", e, now)
            return
        finally:
            self.state.captured.add((r.race_id, "odds", lab))
        if off in CARD_OFFSETS:
            for typ, fn in (("lines", self.client.lines), ("entries", self.client.entries)):
                if (r.race_id, typ, lab) in self.state.captured:
                    continue
                try:
                    data = fn(r.race_id)
                    rec = {"type": typ, **base, "fetched_at": self.clock().astimezone(JST).isoformat(timespec="microseconds"), "data": data}
                    if typ == "lines":
                        rec["parsed"] = parse_lines(data if isinstance(data, dict) else {})
                    append(self.out_dir, r.kaisai_date, rec)
                    self.counts[typ] += 1
                except Exception as e:  # noqa: BLE001
                    self.record_error(r.kaisai_date, r.race_id, f"{typ}{lab}", e, now)
                self.state.captured.add((r.race_id, typ, lab))

    def fetch_pre_cutoff(self, r: Race, off: float, target: datetime, base: dict) -> None:
        """Additional odds only, with no late catch-up and no ordinary retry loop."""
        started=self.clock();wait_started=time.monotonic();deadline=target+timedelta(seconds=PRE_CUTOFF_GRACE_SECONDS)
        if started>=deadline:
            self.miss(r,off,target,started,'precutoff_budget_expired')
            return
        if self.pre_capture is None:
            self.miss(r,off,target,started,'precutoff_worker_unavailable')
            return
        budget=min(PRE_CUTOFF_BUDGET_SECONDS,(deadline-started).total_seconds())
        try:
            od,completed=self.pre_capture.request(r.race_id,budget_seconds=budget,deadline=deadline)
            got=datetime.fromisoformat(completed)
            if got.tzinfo is None or not started<=got<=self.clock()<=deadline:
                raise TimeoutError('Additional capture completed outside its reserved window')
            cutoff=r.close-timedelta(minutes=PRE_CUTOFF_HORIZONS[off])
            if got>=cutoff or not isinstance(od,dict) or od.get('official_dt'):
                raise ValueError('Additional capture is late, final or malformed')
            append(self.out_dir,r.kaisai_date,{'type':'odds',**base,
                'capture_class':'additional_pre_cutoff','attempt_started_at':started.astimezone(JST).isoformat(timespec='microseconds'),
                'nominal_cutoff_at':iso(cutoff),'capture_budget_seconds':budget,
                'attempt_finished_at':self.clock().astimezone(JST).isoformat(timespec='microseconds'),
                'capture_wait_seconds':time.monotonic()-wait_started,
                'fetched_at':got.astimezone(JST).isoformat(timespec='microseconds'),'data':od})
            self.state.captured.add((r.race_id,'odds',label_of(off)));self.counts['odds']+=1
        except Exception as error:
            self.record_error(r.kaisai_date,r.race_id,'precutoff'+label_of(off),error,self.clock())
            self.miss(r,off,target,self.clock(),'precutoff_capture_failed',detail={
                'attempt_started_at':started.astimezone(JST).isoformat(timespec='microseconds'),
                'capture_budget_seconds':budget,'capture_wait_seconds':time.monotonic()-wait_started})

    def miss(self, r: Race, off: float, target: datetime, now: datetime, reason: str, detail=None) -> None:
        lab = label_of(off)
        self.state.captured.add((r.race_id, "odds", lab))
        if now - target > timedelta(minutes=MISS_RECORD_WINDOW_MIN):
            return  # 古い予定時刻（ロガーが動いていなかった時間帯）は記録しない
        append(self.out_dir, r.kaisai_date, {"type": "miss", "race_id": r.race_id, "label": lab,
                                             "target_at": iso(target), "noticed_at": iso(now), "reason": reason,
                                             "capture_schedule_version": CAPTURE_SCHEDULE_VERSION,
                                             **({'detail':detail} if detail is not None else {})})
        self.counts["miss"] += 1

    def run(self) -> dict:
        t0 = self.clock()
        end = t0 + self.window
        self.state = load_state(self.out_dir, self.days_to_watch(t0))
        self.refresh(t0)
        while True:
            now = self.clock()
            if self.last_refresh is None or (now - self.last_refresh).total_seconds() >= RACELIST_REFRESH_SEC:
                self.refresh(now)
            due, upcoming = [], []
            if self.forecaster is not None:
                try:
                    upcoming.extend(self.forecaster.tick(self.races.values(),now,end))
                except Exception as error:
                    self.record_error(now.strftime('%Y%m%d'),None,'research_prediction',error,self.clock())
                    self.forecaster.close();self.forecaster=None
            for t, r, off in self.events(now, end):
                if off < 0:
                    if now > r.close:  # 締切を過ぎた締切前の時点は取らない
                        self.miss(r, off, t, now, "after_close")
                        continue
                    if t <= now:
                        if now - t <= timedelta(minutes=tolerance_min(off)):
                            due.append((t, r, off))
                        else:
                            self.miss(r, off, t, now, "late_start")
                    else:
                        upcoming.append(t)
                else:
                    (due if t <= now else upcoming).append((t, r, off) if t <= now else t)
            # 追加枠は既存枠のcatchup（特に-7の出走/並び）を置き換えない。
            # 既存枠の中では、従来どおり最も締切に近い1回だけ取る。
            by_race: dict[str, tuple] = {}
            for t, r, off in due:
                prior=by_race.get(r.race_id)
                if prior is None or (off not in PRE_CUTOFF_HORIZONS,t) > (prior[2] not in PRE_CUTOFF_HORIZONS,prior[0]):
                    by_race[r.race_id] = (t, r, off)
            for t, r, off in due:
                if by_race[r.race_id][2] != off:
                    reason='precutoff_yielded_existing' if off in PRE_CUTOFF_HORIZONS else 'merged_catchup'
                    self.miss(r, off, t, now, reason)
            for t, r, off in sorted(by_race.values(), key=lambda x: x[0]):
                self.fetch(r, off, t, self.clock())
            now = self.clock()
            if now >= end and not by_race:
                break
            nxt = min([u for u in upcoming if u > now] + [end, now + timedelta(seconds=RACELIST_REFRESH_SEC)])
            wait = (nxt - now).total_seconds()
            if wait > 0:
                self.sleep(min(wait, 60))
        summary = {"start": iso(t0), "end": iso(self.clock()), "races_known": len(self.races), **self.counts}
        self.log(json.dumps(summary, ensure_ascii=False))
        return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--window-min", type=float, default=12.0)
    ap.add_argument("--research-bundle",action='append',default=[],help="Optional frozen bundle; repeat for independently frozen cohorts")
    ap.add_argument("--research-manifest",help="Prepared mapping of cohort names to frozen bundle paths")
    a = ap.parse_args(argv)
    forecaster=None
    bundles=list(a.research_bundle)
    if a.research_manifest:
        with open(a.research_manifest,encoding='utf-8') as f:bundles.extend(json.load(f).values())
    if bundles:
        from research_runtime import ResearchGroup
        forecaster=ResearchGroup(list(dict.fromkeys(bundles)),a.out_dir)
    from precutoff_capture import TimedOddsCapture
    pre_capture=TimedOddsCapture()
    try:
        summary=Logger(NetkeirinClient(), a.out_dir, a.window_min,forecaster=forecaster,pre_capture=pre_capture).run()
        # A normal process exit does not imply a healthy capture. Preserve the
        # run counts for the independent post-save health gate.
        day=datetime.fromisoformat(summary['start']).astimezone(JST).strftime('%Y%m%d')
        run_id=os.environ.get('GITHUB_RUN_ID',uuid.uuid4().hex)
        if not str(run_id).replace('-','').isalnum():raise ValueError('Invalid run identifier')
        receipt={'schema_version':'capture-summary-v1','run_id':run_id,
                 'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT','1'),**summary}
        os.makedirs(a.out_dir,exist_ok=True)
        with open(os.path.join(a.out_dir,f'logger_summary_{day}_{run_id}.json'),'w',encoding='utf-8') as stream:
            json.dump(receipt,stream,ensure_ascii=False,allow_nan=False)
    finally:
        pre_capture.close()
        if forecaster is not None:forecaster.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
