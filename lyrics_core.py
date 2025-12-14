#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import glob
import os
import tempfile
from typing import Any, Dict, List, Tuple

import yt_dlp


def _cookie_file() -> str | None:
    """
    YT の cookie ファイルのパスを推測する。
    優先順位:
      1. 環境変数 YT_COOKIES_FILE
      2. 環境変数 YOUTUBE_COOKIES_FILE（過去互換）
      3. リポジトリルートの youtube_cookies.txt
    """
    for key in ("YT_COOKIES_FILE", "YOUTUBE_COOKIES_FILE"):
        path = os.environ.get(key)
        if path and os.path.exists(path):
            return path

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path2 = os.path.join(repo_root, "youtube_cookies.txt")
    if os.path.exists(path2):
        return path2

    return None


def _base_ydl_opts() -> Dict[str, Any]:
    """共通の yt-dlp オプション"""
    opts: Dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
        "noplaylist": True,
    }
    cookie = _cookie_file()
    if cookie:
        opts["cookiefile"] = cookie
    return opts


def _download_auto_sub_srt(video_id: str) -> str:
    """
    yt-dlp を使って自動生成字幕を SRT 形式で一時ディレクトリに保存し、そのパスを返す。
    ※ yt-dlp は「<id>.<lang>.srt」などで吐くことがあるので glob で拾う。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory(prefix="yt_sub_") as tmp_dir:
        # outtmpl は拡張子や言語が付くことがあるので「ベース名」だけ指定
        outtmpl = os.path.join(tmp_dir, "%(id)s")

        ydl_opts = _base_ydl_opts()
        ydl_opts.update(
            {
                "writeautomaticsub": True,
                "subtitlesformat": "srt",
                "skip_download": True,
                "outtmpl": outtmpl,
            }
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # 生成された srt を拾う（複数言語が出たら一番大きいものを採用）
        candidates = glob.glob(os.path.join(tmp_dir, "*.srt"))
        if not candidates:
            raise RuntimeError("SRT 字幕ファイルが生成されませんでした。")

        best = max(candidates, key=lambda p: os.path.getsize(p))
        # tmp_dir が消える前に内容を別ファイルへコピーして返す
        final_path = os.path.join(tempfile.gettempdir(), f"{video_id}.auto.srt")
        with open(best, "rb") as src, open(final_path, "wb") as dst:
            dst.write(src.read())

        return final_path


def _srt_to_lyrics(path: str) -> str:
    """
    SRT ファイルから歌詞テキストだけを抜き出す (タイムスタンプと番号行は削除)。
    """
    lines: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.isdigit():
                continue
            if "-->" in line:
                continue
            lines.append(line)

    merged: List[str] = []
    last: str | None = None
    for line in lines:
        if line == last:
            continue
        merged.append(line)
        last = line

    return "\n".join(merged)


def search_lyrics_candidates(*args, **kwargs) -> List[Dict[str, Any]]:
    """
    互換性用のダミー実装（現状未使用）
    """
    return []


def register_lyrics_from_request(
    artist: str,
    title: str,
    video_id: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    必須: YouTube の動画 ID。
    自動生成字幕から歌詞を取得して (lyrics, video_id, info) を返す。
    """
    srt_path = _download_auto_sub_srt(video_id)
    lyrics = _srt_to_lyrics(srt_path)

    info: Dict[str, Any] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "artist": artist,
        "title": title,
    }
    return lyrics, video_id, info


def format_lyrics_for_issue_body(
    artist: str,
    title: str,
    lyrics: str,
    video_url: str | None = None,
) -> str:
    """
    GitHub Issue のコメント用に歌詞を整形する。
    """
    header = f"**{artist} - {title}**"
    if video_url:
        header += f"\n\n[YouTube]({video_url})"

    body = f"{header}\n\n```text\n{lyrics}\n```"
    return body
