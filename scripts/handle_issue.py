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
sys.path.insert(0, str(ROOT_DIR))  # ルートの lyrics_core.py を import できるようにする

import lyrics_core  # noqa: E402


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


# ---------- コメント生成 ----------

JSON_START = "<!-- LYRICS_API_JSON_START -->"
JSON_END = "<!-- LYRICS_API_JSON_END -->"


def _looks_like_lyrics(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # 2行以上、かつ少しは長いものだけを「成功」とみなす（短すぎる誤爆を回避）
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
    chosen_source: str,  # "youtube" | "lrclib" | "none"
    youtube_lyrics: Optional[str],
    youtube_info: Optional[Dict[str, Any]],
    lrclib_rec: Optional[Dict[str, Any]],
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
        lines.append("- ステータス: Auto（YouTube 自動字幕）")
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
            status = "Auto（同期あり）"
        elif plain:
            status = "Auto（同期なし）"
        else:
            status = "歌詞の登録なし"

        lines.append(f"- ステータス: {status}")
        lines.append("- 取得元: LRCLIB")

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

    else:
        lines.append("- ステータス: 歌詞の取得に失敗しました")
        lines.append("- 取得元: YouTube → LRCLIB（どちらも失敗）")

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

    # opened/edited/reopened/labeled を処理
    if action not in {"opened", "edited", "reopened", "labeled"}:
        print("対象外アクションなのでスキップします。")
        return

    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    chosen_source = "none"
    youtube_lyrics: Optional[str] = None
    youtube_info: Optional[Dict[str, Any]] = None
    lrclib_rec: Optional[Dict[str, Any]] = None

    # 1) YouTube（動画IDがある時だけ）
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
        if lrclib_rec:
            chosen_source = "lrclib"
            print("[lrclib] record found:", lrclib_rec.get("id"), lrclib_rec.get("trackName"), lrclib_rec.get("artistName"))
        else:
            chosen_source = "none"
            print("[lrclib] no record found")

    comment_body = build_comment_body(
        artist=artist,
        title=title,
        video_id=video_id,
        chosen_source=chosen_source,
        youtube_lyrics=youtube_lyrics,
        youtube_info=youtube_info,
        lrclib_rec=lrclib_rec,
    )
    comment_to_issue(repo, issue_number, comment_body)
    print("comment posted.")


if __name__ == "__main__":
    main()
