"""Kドリームスのレース詳細ページと、オッズパークの出走表ページから、必要な部分だけを取り出す。

方針：ページ全体（1件30万字前後）は保存せず、表は「行ごとのセルの文字列」、節は本文の文字列として残す。
列の意味づけは後の分析側で行う（ページの列構成が年によって変わっても、取り直さずに済むように）。
"""
from __future__ import annotations

from bs4 import BeautifulSoup


def _rows(table) -> list[list[str]]:
    out = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if any(cells):
            out.append(cells)
    return out


def _section_text(soup, tag: str, title: str) -> str | None:
    for h in soup.find_all(tag, class_="title"):
        if title in h.get_text():
            box = h.find_parent()
            text = box.get_text(" ", strip=True) if box else ""
            return text[len(title):].strip() if text.startswith(title) else text
    return None


def parse_kdreams(html: bytes | str) -> dict:
    """Kドリームスのレース詳細（racedetail）。

    racecard  出走表の表（予想印・好気合・総評・直近4か月・選手コメント・前回成績と走り評／初日は短評・今場所/前場所/前々場所 など）
    tipster   記者の予想・周回予想・選手コメント
    result    結果（着順・着差・上り・決まり手・S/B・勝敗因）
    refund    払戻
    nerai     記者の狙い目
    after     「戦い終わって」（レース後の経過と選手の談話）
    """
    s = BeautifulSoup(html, "lxml")
    rec: dict = {}
    t = s.find("title")
    rec["page_title"] = t.get_text(strip=True) if t else None
    h2 = s.select_one("h2.title")
    rec["headline"] = h2.get_text(" ", strip=True) if h2 else None
    rec["racecard"] = [_rows(x) for x in s.find_all("table", class_="racecard_table")]
    for key, cls in (("tipster", "tipster_table"), ("result", "result_table"), ("refund", "refund_table")):
        x = s.find("table", class_=cls)
        rec[key] = _rows(x) if x else None
    rec["nerai"] = _section_text(s, "h3", "狙い目")
    rec["after"] = _section_text(s, "h4", "戦い終わって")
    rec["ok"] = bool(rec["racecard"])
    return rec


def parse_oddspark(html: bytes | str) -> dict:
    """オッズパークの出走表（SpRaceInfo）。選手コメントの表（commentTable）を取り出す。"""
    s = BeautifulSoup(html, "lxml")
    rec: dict = {}
    t = s.find("title")
    rec["page_title"] = t.get_text(strip=True) if t else None
    x = s.find("table", class_="commentTable")
    rec["comments"] = _rows(x) if x else None
    rec["ok"] = x is not None
    return rec
