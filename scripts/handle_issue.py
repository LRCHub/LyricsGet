#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from github import Github, Auth

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))  # ルート直下のモジュールを import できるように

import lyrics_core   # YouTube 自動字幕
import pl            # PetitLyrics
import uta           # UtaTen


# ---------- GitHub イベント読み込み ----------

def load_github_event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("環境変数 GITHUB_EVENT_PATH が設定されていません。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Issue 本文パース（パターンA） ----------

YOUTUBE_PATTERNS = [
    r"(?:https?://)?(?:www\.)?youtu\.be/([0-9A-Za-z_-]{8,})",
    r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([0-9A-Za-z_-]{8,})",
    r"(?:https?://)?(?:www\.)?youtube\.com/shorts/([0-9A-Za-z_-]{8,})",
]


def extract_video_id_from_text(text: str) -> Optional[str]:
    for pat in YOUTUBE_PATTERNS:
        m = re.search(pat, text or "")
        if m:
            vid = (m.group(1) or "").strip()
            if vid:
                return vid
    return None


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    パターンA:
      1行目: "アーティスト - タイトル"
      2行目以降: 任意。YouTubeリンクがあれば動画IDを取る。

    戻り値: (artist, title, video_id)
    """
    artist: Optional[str] = None
    title: Optional[str] = None

    lines = [line.strip() for line in (body or "").splitlines()]

    for line in lines:
        if not line:
            continue
        if " - " in line:
            left, right = line.split(" - ", 1)
            artist = (left or "").strip() or None
            title = (right or "").strip() or None
            break

    video_id = extract_video_id_from_text(body or "")
    return artist, title, video_id


# ---------- LRCLIB ----------

LRC_LIB_BASE = "https://lrclib.net"


def _nf_lrc(s: str) -> str:
    import unicodedata as u
    t = u.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def search_lrclib_by_artist_title(
    artist: Optional[str],
    title: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    LRCLIB /api/search を叩いて最も良さそうな1件を返す。
    """
    if not artist and not title:
        return None

    params: Dict[str, str] = {}
    if title:
        params["track_name"] = title
    if artist:
        params["artist_name"] = artist

    if not params:
        return None

    try:
        r = requests.get(f"{LRC_LIB_BASE}/api/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[lrclib] search error: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    def score(rec: Dict[str, Any]) -> int:
        s = 0
        if title and rec.get("trackName"):
            s += 2 * (100 - abs(len(_nf_lrc(title)) - len(_nf_lrc(str(rec["trackName"])))))
        if artist and rec.get("artistName"):
            s += 2 * (100 - abs(len(_nf_lrc(artist)) - len(_nf_lrc(str(rec["artistName"])))))
        return s

    return max(data, key=score)


def _lrclib_has_lyrics(rec: Optional[Dict[str, Any]]) -> bool:
    if not rec:
        return False
    plain = (rec.get("plainLyrics") or "").strip()
    synced = (rec.get("syncedLyrics") or "").strip()
    return bool(plain or synced)


# ---------- コメント生成 ----------

JSON_START = "<!-- LYRICS_API_JSON_START -->"
JSON_END = "<!-- LYRICS_API_JSON_END -->"


def _looks_like_lyrics(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if len(lines) < 2:
        return False
    if len(t) < 20:
        return False
    return True


def build_comment_body(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    chosen_source: str,  # "youtube" | "lrclib" | "petitlyrics" | "utaten" | "none"
    youtube_lyrics: Optional[str],
    youtube_info: Optional[Dict[str, Any]],
    lrclib_rec: Optional[Dict[str, Any]],
    petit_lyrics: Optional[str],
    petit_meta: Optional[Dict[str, Any]],
    utaten_lyrics: Optional[str],
    utaten_meta: Optional[Dict[str, Any]],
) -> str:
    lines: list[str] = []

    lines.append("自動歌詞登録の結果をお知らせします 🤖\n")

    # 解析結果
    lines.append("### 解析結果")
    lines.append(f"- アーティスト: **{artist}**" if artist else "- アーティスト: (未入力)")
    lines.append(f"- 楽曲名: **{title}**" if title else "- 楽曲名: (未入力)")
    lines.append(f"- 動画 ID: `{video_id}`" if video_id else "- 動画 ID: (未指定)")

    lines.append("\n### 歌詞登録結果")

    if chosen_source == "youtube" and youtube_lyrics:
        lines.append("- ステータス: 自動登録（YouTube 自動字幕）")
        lines.append("- 取得元: YouTube（自動字幕）")
        if youtube_info and youtube_info.get("url"):
            lines.append(f"- 参照: {youtube_info['url']}")
        lines.append("\n#### 歌詞（テキスト）")
        lines.append("```text")
        lines.append(youtube_lyrics.strip())
        lines.append("```")

    elif chosen_source == "lrclib" and lrclib_rec:
        plain = (lrclib_rec.get("plainLyrics") or "").strip()
        synced = (lrclib_rec.get("syncedLyrics") or "").strip()

        if synced:
            status = "自動登録（同期あり）"
        elif plain:
            status = "自動登録（同期なし）"
        else:
            status = "歌詞の登録なし"

        lines.append(f"- ステータス: {status}")
        lines.append("- 取得元: 外部歌詞データベース")

        tn = (lrclib_rec.get("trackName") or lrclib_rec.get("name") or "").strip()
        an = (lrclib_rec.get("artistName") or "").strip()
        detail = []
        if tn:
            detail.append(f"track='{tn}'")
        if an:
            detail.append(f"artist='{an}'")
        if detail:
            lines.append(f"- 取得詳細: {', '.join(detail)}")

        if synced:
            lines.append("\n#### syncedLyrics（タイミング付き）")
            lines.append("```lrc")
            lines.append(synced)
            lines.append("```")

        if plain:
            lines.append("\n#### plainLyrics（テキストのみ）")
            lines.append("```text")
            lines.append(plain)
            lines.append("```")

        if (not synced) and (not plain):
            lines.append("- 歌詞が空でした。")

    elif chosen_source == "petitlyrics" and petit_lyrics:
        lines.append("- ステータス: 自動登録（テキストのみ）")
        lines.append("- 取得元: 歌詞サイト（その1）")
        if petit_meta:
            detail = []
            if petit_meta.get("title"):
                detail.append(f"title='{petit_meta['title']}'")
            if petit_meta.get("artist"):
                detail.append(f"artist='{petit_meta['artist']}'")
            if petit_meta.get("song_url"):
                detail.append(f"url={petit_meta['song_url']}")
            if detail:
                lines.append(f"- 取得詳細: {', '.join(detail)}")

        lines.append("\n#### 歌詞（テキスト）")
        lines.append("```text")
        lines.append(petit_lyrics.strip())
        lines.append("```")

    elif chosen_source == "utaten" and utaten_lyrics:
        lines.append("- ステータス: 自動登録（テキストのみ）")
        lines.append("- 取得元: 歌詞サイト（その2）")
        if utaten_meta:
            detail = []
            if utaten_meta.get("title"):
                detail.append(f"title='{utaten_meta['title']}'")
            if utaten_meta.get("artist"):
                detail.append(f"artist='{utaten_meta['artist']}'")
            if utaten_meta.get("url"):
                detail.append(f"url={utaten_meta['url']}")
            if detail:
                lines.append(f"- 取得詳細: {', '.join(detail)}")

        lines.append("\n#### 歌詞（テキスト）")
        lines.append("```text")
        lines.append(utaten_lyrics.strip())
        lines.append("```")

    else:
        lines.append("- ステータス: 歌詞の取得に失敗しました")
        lines.append("- 取得元: YouTube → 外部DB → 歌詞サイト（複数）いずれも取得不可")

    # 機械用ペイロード
    payload: Dict[str, Any] = {
        "videoId": video_id,
        "artist": artist,
        "title": title,
        "chosenSource": chosen_source,
        "youtube": {
            "lyrics": youtube_lyrics,
            "info": youtube_info,
        },
        "lrclib": {
            "record": lrclib_rec,
        },
        "petitlyrics": {
            "lyrics": petit_lyrics,
            "meta": petit_meta,
        },
        "utaten": {
            "lyrics": utaten_lyrics,
            "meta": utaten_meta,
        },
    }

    lines.append("\n---")
    lines.append("以下はローカルスクリプト用のペイロードです（編集しないでください）。")
    lines.append(JSON_START)
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append(JSON_END)

    lines.append("\n※ このコメントは GitHub Actions の自動処理で追加されています。")
    return "\n".join(lines)


def comment_to_issue(repo, issue_number: int, body: str) -> None:
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ---------- メイン ----------

def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPOSITORY")

    if not token:
        raise RuntimeError("環境変数 GITHUB_TOKEN が設定されていません。")
    if not repo_name:
        raise RuntimeError("環境変数 GITHUB_REPOSITORY が設定されていません。")

    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_name)

    event = load_github_event()
    action = event.get("action")
    issue_data = event.get("issue")

    if not issue_data:
        print("issue イベントではないため何もしません。")
        return

    issue_number = issue_data["number"]
    issue_body = issue_data.get("body") or ""

    print(f"action={action}, issue_number={issue_number}")

    if action not in {"opened", "edited", "reopened", "labeled"}:
        print("対象外アクションなのでスキップします。")
        return

    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    chosen_source = "none"
    youtube_lyrics: Optional[str] = None
    youtube_info: Optional[Dict[str, Any]] = None
    lrclib_rec: Optional[Dict[str, Any]] = None
    petit_lyrics: Optional[str] = None
    petit_meta: Optional[Dict[str, Any]] = None
    utaten_lyrics: Optional[str] = None
    utaten_meta: Optional[Dict[str, Any]] = None

    # 1) YouTube（動画IDがある場合のみ）
    if video_id:
        try:
            y_lyrics, y_vid, y_info = lyrics_core.register_lyrics_from_request(
                artist or "",
                title or "",
                video_id,
            )
            if _looks_like_lyrics(y_lyrics):
                chosen_source = "youtube"
                youtube_lyrics = y_lyrics
                youtube_info = y_info
                print("[youtube] lyrics ok")
            else:
                print("[youtube] lyrics empty/too short -> fallback to LRCLIB")
        except Exception as e:
            print(f"[youtube] error: {e} -> fallback to LRCLIB")

    # 2) LRCLIB（YouTube が成功しなかった時）
    if chosen_source != "youtube":
        lrclib_rec = search_lrclib_by_artist_title(artist, title)
        if _lrclib_has_lyrics(lrclib_rec):
            chosen_source = "lrclib"
            print("[lrclib] record with lyrics found:", lrclib_rec.get("id"), lrclib_rec.get("trackName"), lrclib_rec.get("artistName"))
        else:
            if lrclib_rec:
                print("[lrclib] record found but lyrics empty")
            else:
                print("[lrclib] no record found")

    # 3) PetitLyrics（YouTube & LRCLIB どちらもダメなとき）
    if chosen_source not in {"youtube", "lrclib"}:
        if artist or title:
            try:
                petit_lyrics, petit_meta = pl.fetch_petitlyrics(
                    title or "",
                    artist or "",
                    sleep_sec=1.0,
                )
                if _looks_like_lyrics(petit_lyrics):
                    chosen_source = "petitlyrics"
                    print("[petitlyrics] lyrics ok:", petit_meta)
                else:
                    print("[petitlyrics] lyrics empty/too short -> try UtaTen")
            except Exception as e:
                print(f"[petitlyrics] error: {e} -> try UtaTen")
        else:
            print("[petitlyrics] skipped (artist/title が空)")

    # 4) UtaTen（YouTube & LRCLIB & プチリリ すべてダメなとき）
    if chosen_source not in {"youtube", "lrclib", "petitlyrics"}:
        if artist or title:
            try:
                utaten_lyrics, utaten_meta = uta.fetch_utaten(
                    title or "",
                    artist or "",
                    sleep_sec=1.0,
                )
                if _looks_like_lyrics(utaten_lyrics):
                    chosen_source = "utaten"
                    print("[utaten] lyrics ok:", utaten_meta)
                else:
                    print("[utaten] lyrics empty/too short")
            except Exception as e:
                print(f"[utaten] error: {e}")
        else:
            print("[utaten] skipped (artist/title が空)")

    comment_body = build_comment_body(
        artist=artist,
        title=title,
        video_id=video_id,
        chosen_source=chosen_source,
        youtube_lyrics=youtube_lyrics,
        youtube_info=youtube_info,
        lrclib_rec=lrclib_rec,
        petit_lyrics=petit_lyrics,
        petit_meta=petit_meta,
        utaten_lyrics=utaten_lyrics,
        utaten_meta=utaten_meta,
    )
    comment_to_issue(repo, issue_number, comment_body)
    print("comment posted.")


if __name__ == "__main__":
    main()
