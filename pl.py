#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

BASE = "https://petitlyrics.com"


@dataclass
class SearchHit:
    lyrics_id: int
    title: str
    artist: Optional[str]
    song_url: str


def _normalize_key(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = s.replace("　", " ")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[\"'’`“”\(\)\[\]\{\}<>【】（）［］｛｝・/\\\-\–—:：,，\.。!！\?？~〜]", "", s)
    return s


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"{BASE}/search_lyrics",
        "Connection": "keep-alive",
    })
    return s


def warmup_session(s: requests.Session, sleep_sec: float = 0.2) -> None:
    try:
        s.get(f"{BASE}/", timeout=20, allow_redirects=True)
        time.sleep(sleep_sec)
        s.get(f"{BASE}/search_lyrics", timeout=20, allow_redirects=True)
        time.sleep(sleep_sec)
    except Exception:
        pass


def build_search_url(title: str, artist: str) -> str:
    params: Dict[str, str] = {
        "title": title,
        "title_opt": "",
        "artist": artist,
        "artist_opt": "",
    }
    return f"{BASE}/search_lyrics?{urlencode(params, doseq=False)}"


def parse_search_results(html: str) -> List[SearchHit]:
    soup = BeautifulSoup(html, "html.parser")

    # /lyrics/<数字> だけを曲ページとして拾う
    song_links = soup.find_all("a", href=re.compile(r"^/lyrics/\d+$"))

    hits: List[SearchHit] = []
    seen = set()

    for a in song_links:
        href = a.get("href", "")
        m = re.search(r"/lyrics/(\d+)$", href)
        if not m:
            continue

        lyrics_id = int(m.group(1))
        if lyrics_id in seen:
            continue
        seen.add(lyrics_id)

        # 行っぽい親要素をたどって artist を取る
        row = a
        for _ in range(10):
            if row is None:
                break
            if getattr(row, "find", None):
                if row.find("a", href=re.compile(r"^/lyrics/artist/")):
                    break
            row = row.parent

        title = a.get_text(strip=True)
        song_url = BASE + href

        artist_a = row.find("a", href=re.compile(r"^/lyrics/artist/")) if getattr(row, "find", None) else None
        artist = artist_a.get_text(strip=True) if artist_a else None

        hits.append(SearchHit(
            lyrics_id=lyrics_id,
            title=title,
            artist=artist,
            song_url=song_url
        ))

    return hits


def choose_best_hit(hits: List[SearchHit], req_title: str, req_artist: str) -> Optional[SearchHit]:
    if not hits:
        return None

    nt = _normalize_key(req_title)
    na = _normalize_key(req_artist)

    exact = []
    for h in hits:
        ok_t = _normalize_key(h.title) == nt if nt else True
        ok_a = _normalize_key(h.artist) == na if na else True
        if ok_t and ok_a:
            exact.append(h)

    cand = exact if exact else hits
    # “最新扱い”で lyrics_id 最大を選ぶ
    return max(cand, key=lambda x: x.lyrics_id)


def extract_lyrics_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    # よくある候補
    for sel in ("#lyrics", ".lyrics", ".lyricsBody", ".lyrics-body", "#lyric", ".lyric"):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text("\n", strip=True)
            if len(txt) > 30:
                return txt

    # それっぽい区間抽出（Bookmark〜Posted By）
    start = soup.find(string=re.compile(r"Bookmark this page|☆Bookmark|ブックマーク", re.I))
    if start:
        chunks: List[str] = []
        stop_pat = re.compile(r"Purchase on|Lyrics List For This Artist|Posted By:|URL of this page|このページのURL", re.I)

        for node in start.parent.next_elements:
            if isinstance(node, str):
                t = node.strip()
                if not t:
                    continue
                if stop_pat.search(t):
                    break
                if t in ("Tweet", "TOP", "Lyric Search", "歌詞検索"):
                    continue
                chunks.append(t)

        text = "\n".join(chunks).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) > 30:
            return text

    return soup.get_text("\n", strip=True)


def fetch_lyrics_only(s: requests.Session, lyrics_id: int, sleep_sec: float = 1.0) -> str:
    url = f"{BASE}/lyrics/{lyrics_id}"
    r = s.get(url, timeout=20, allow_redirects=True)

    if "petitlyrics.com/404.php" in r.url:
        raise RuntimeError(f"歌詞ページが 404.php に飛びました: {url} -> {r.url}")

    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    time.sleep(sleep_sec)
    return extract_lyrics_text(r.text)


# ---------- 外部から呼び出す用の関数 ----------

def fetch_petitlyrics(title: str, artist: str, sleep_sec: float = 1.0) -> Tuple[str, Dict[str, Optional[str]]]:
    """
    指定したタイトル / アーティストで PetitLyrics を検索し、
    歌詞本文とメタ情報を返す。
    戻り値: (lyrics, meta)
    meta には lyrics_id, title, artist, song_url, search_url が入る。
    """
    s = _session()
    warmup_session(s, sleep_sec=min(0.3, sleep_sec))

    search_url = build_search_url(title, artist)
    r = s.get(search_url, timeout=20, allow_redirects=True)

    if "petitlyrics.com/404.php" in r.url:
        raise RuntimeError(f"検索が 404.php に飛びました: {search_url} -> {r.url}")

    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    hits = parse_search_results(r.text)
    best = choose_best_hit(hits, title, artist)
    if not best:
        raise RuntimeError("一致する曲が見つかりませんでした。")

    lyrics = fetch_lyrics_only(s, best.lyrics_id, sleep_sec=sleep_sec)
    meta: Dict[str, Optional[str]] = {
        "lyrics_id": str(best.lyrics_id),
        "title": best.title,
        "artist": best.artist,
        "song_url": best.song_url,
        "search_url": search_url,
    }
    return lyrics, meta


# ---------- CLI エントリポイント ----------

def main():
    # Windowsの文字化け対策（できる環境だけ）
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True, help="曲名")
    ap.add_argument("--artist", required=True, help="アーティスト名")
    ap.add_argument("--sleep", type=float, default=1.0, help="アクセス間隔(秒)")
    ap.add_argument("--lyrics-only", action="store_true", help="歌詞本文だけ出力")
    args = ap.parse_args()

    try:
        lyrics, meta = fetch_petitlyrics(args.title, args.artist, sleep_sec=args.sleep)
    except RuntimeError as e:
        raise SystemExit(str(e))

    # 最小版なので、--lyrics-only の有無に関わらず歌詞本文だけ出す
    print(lyrics)


if __name__ == "__main__":
    main()
