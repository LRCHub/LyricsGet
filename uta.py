#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UtaTen (utaten.com)
- title + artist で検索して 1件だけ選ぶ
- 歌詞本文だけを stdout に出す（ふりがな/ローマ字を除去）

モジュールとしては:
    fetch_utaten(title, artist, sleep_sec=1.0) -> (lyrics, meta)
も提供する。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag, NavigableString

BASE = "https://utaten.com"


@dataclass
class SearchHit:
    url: str
    title: str
    artist: Optional[str]


# ===== 判定ユーティリティ =====
_RE_KANJI = re.compile(r"[\u4E00-\u9FFF]")
_RE_JP = re.compile(r"[\u3040-\u30FF\u4E00-\u9FFF]")
_RE_LATIN_SEQ = re.compile(r"[A-Za-z]{2,}")  # \b だと和文との境界で拾えないのでこれを使う
_RE_LATIN_ONLY = re.compile(r"^[A-Za-z]+$")


def has_kanji(s: str) -> bool:
    return bool(_RE_KANJI.search(s))


def has_japanese(s: str) -> bool:
    return bool(_RE_JP.search(s))


def is_kana_only(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        if ("\u3040" <= ch <= "\u30FF") or ch in "ー゛゜":
            continue
        return False
    return True


def _normalize_key(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip().lower().replace("　", " ")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[\"'’`“”\(\)\[\]\{\}<>【】（）［］｛｝・/\\\-\–—:：,，\.。!！\?？~〜]", "", s)
    return s


def latin_heavy(line: str) -> bool:
    """ローマ字ブロック判定：英字の塊が多い行に入ったらそこで止める"""
    chunks = _RE_LATIN_SEQ.findall(line)
    total = sum(len(c) for c in chunks)
    # 普通の歌詞で英字が少し出る程度なら切らないように、少し強めの閾値
    return (total >= 12 and len(chunks) >= 4)


# ===== HTTP =====
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"{BASE}/search",
        "Connection": "keep-alive",
    })
    return s


def warmup_session(s: requests.Session, sleep_sec: float = 0.25) -> None:
    try:
        s.get(f"{BASE}/", timeout=20, allow_redirects=True)
        time.sleep(sleep_sec)
        s.get(f"{BASE}/search", timeout=20, allow_redirects=True)
        time.sleep(sleep_sec)
    except Exception:
        pass


def fetch_html(s: requests.Session, url: str, timeout: int = 25) -> str:
    r = s.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


# ===== Search =====
def build_search_url(title: str, artist: str) -> str:
    params: Dict[str, str] = {
        "sort": "popular_sort_asc",
        "artist_name": artist,
        "title": title,
    }
    return f"{BASE}/search?{urlencode(params, doseq=False)}"


def parse_search_results(html: str) -> List[SearchHit]:
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=re.compile(r"^/lyric/[^/]+/"))

    hits: List[SearchHit] = []
    seen = set()

    for a in links:
        href = a.get("href") or ""
        url = BASE + href
        if url in seen:
            continue
        seen.add(url)

        title = a.get_text(strip=True)
        if not title:
            continue

        row = a
        for _ in range(12):
            if row is None:
                break
            if getattr(row, "find", None) and row.find("a", href=re.compile(r"^/artist/\d+/")):
                break
            row = row.parent

        artist_a = row.find("a", href=re.compile(r"^/artist/\d+/")) if getattr(row, "find", None) else None
        artist = artist_a.get_text(strip=True) if artist_a else None

        hits.append(SearchHit(url=url, title=title, artist=artist))

    return hits


def choose_one(hits: List[SearchHit], req_title: str, req_artist: str) -> Optional[SearchHit]:
    if not hits:
        return None

    nt = _normalize_key(req_title)
    na = _normalize_key(req_artist)

    exact: List[SearchHit] = []
    for h in hits:
        ht = _normalize_key(h.title)
        ha = _normalize_key(h.artist)
        ok_t = (ht == nt) if nt else True
        ok_a = (na in ha) if na else True  # feat. 含みを許す
        if ok_t and ok_a:
            exact.append(h)

    return exact[0] if exact else hits[0]


# ===== Lyrics extraction =====
def clean_line_drop_furigana_romaji(line: str) -> str:
    """
    入力例（ページ上の見え方）:
      「昨日人 きのうひと を 殺 ころ したんだ」
      -> 「昨日人を殺したんだ」
    """
    line = re.sub(r"\s+", " ", line).strip()
    toks = line.split(" ")

    out: List[str] = []
    prev_has_kanji = False

    for tok in toks:
        if not tok:
            continue

        # 英字だけのトークンは落とす（wo, koro など）
        if _RE_LATIN_ONLY.match(tok):
            prev_has_kanji = False
            continue

        # 直前が漢字を含むトークンで、次が「かなだけ」ならふりがな扱いで落とす
        if prev_has_kanji and is_kana_only(tok) and len(tok) <= 12:
            prev_has_kanji = False
            continue

        out.append(tok)
        prev_has_kanji = has_kanji(tok)

    return "".join(out).strip()


def extract_lyrics_only(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    # 歌詞の直前にある UI の「ダークモード」付近から拾う
    start = soup.find(string=re.compile(r"ダークモード"))
    if not start:
        start = soup.find(string=re.compile(r"ふりがな"))
    if not start:
        return ""

    stop_pat = re.compile(
        r"(この歌詞へのご意見|みんなのレビュー|レビューを投稿|ブログやHPでこの歌詞を共有|UtaTenはreCAPTCHA|歌詞検索UtaTen)",
        re.I
    )

    # DOM を走査して:
    # - <br> だけ改行
    # - 文字列同士は「スペース」を挟んで繋いで、ふりがな判定できるようにする
    buf: List[str] = []
    last_was_nl = True

    def push_text(txt: str) -> None:
        nonlocal last_was_nl
        txt = txt.replace("\xa0", " ")
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            return
        if not last_was_nl:
            buf.append(" ")
        buf.append(txt)
        last_was_nl = False

    for node in start.next_elements:
        if isinstance(node, NavigableString):
            s = str(node)
            if stop_pat.search(s):
                break
            push_text(s)
        elif isinstance(node, Tag):
            if node.name == "br":
                buf.append("\n")
                last_was_nl = True

    raw = "".join(buf)
    lines = [ln.strip() for ln in raw.split("\n")]

    junk_exact = {
        "文字サイズ", "ふりがな", "ダークモード",
        "歌詞検索", "マイページ",
        "歌詞",
    }

    out_lines: List[str] = []
    for ln in lines:
        if not ln:
            continue
        if ln in junk_exact:
            continue

        # ローマ字ブロックに入ったら終了
        if latin_heavy(ln):
            break

        cl = clean_line_drop_furigana_romaji(ln)
        if not cl:
            continue

        # 日本語が無い短いゴミを捨てる
        if len(cl) <= 2 and not has_japanese(cl):
            continue

        out_lines.append(cl)

    text = "\n".join(out_lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ===== 外部から呼び出す用 =====

def fetch_utaten(title: str, artist: str, sleep_sec: float = 1.0) -> Tuple[str, Dict[str, Optional[str]]]:
    """
    title / artist から UtaTen を検索して歌詞とメタ情報を返す。
    戻り値: (lyrics, meta)
    meta: { "url", "title", "artist", "search_url" }
    """
    s = _session()
    warmup_session(s, sleep_sec=min(0.3, sleep_sec))

    search_url = build_search_url(title, artist)
    search_html = fetch_html(s, search_url)
    hits = parse_search_results(search_html)

    best = choose_one(hits, title, artist)
    if not best:
        raise RuntimeError("検索結果が見つかりませんでした。title/artist を見直してください。")

    time.sleep(sleep_sec)

    lyric_html = fetch_html(s, best.url)
    lyrics = extract_lyrics_only(lyric_html)

    if not lyrics or len(lyrics) < 50:
        raise RuntimeError(
            "歌詞のみ抽出に失敗しました。\n"
            "対策: sleep_sec を増やす / その曲ページURL（/lyric/.../）でHTMLが取れているか確認"
        )

    meta: Dict[str, Optional[str]] = {
        "url": best.url,
        "title": best.title,
        "artist": best.artist,
        "search_url": search_url,
    }
    return lyrics, meta


# ===== CLI =====

def main() -> None:
    # Windows の文字化け対策
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True, help="曲名")
    ap.add_argument("--artist", required=True, help="アーティスト名")
    ap.add_argument("--sleep", type=float, default=1.0, help="アクセス間隔(秒)")
    args = ap.parse_args()

    lyrics, meta = fetch_utaten(args.title, args.artist, sleep_sec=args.sleep)
    print(lyrics)


if __name__ == "__main__":
    main()
