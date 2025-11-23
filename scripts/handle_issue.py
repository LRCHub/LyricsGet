# scripts/handle_issue.py
# 自動歌詞登録: Issue をトリガーにして外部歌詞APIを叩き、その結果を Issue コメントに出すだけのスクリプト
#
# 前提:
#   - GitHub Actions から実行される（GITHUB_EVENT_PATH, GITHUB_REPOSITORY, GITHUB_TOKEN を利用）
#   - Issue 本文 1 行目: 「アーティスト - 曲名」
#   - 本文のどこかに YouTube URL か 動画ID 行 が書かれている想定
#
# 例:
#   YOASOBI - 夜に駆ける
#   https://www.youtube.com/watch?v=by4SYYWlhEs
#
# やっていること:
#   1. Issue 本文から artist / title / video_id を解析
#   2. 外部歌詞API(※コメント内ではサービス名を出さない) を /api/search で叩く
#   3. 結果から「Auto/同期あり / Auto/同期なし / 歌詞の登録なし」を判定
#   4. 解析結果 + 歌詞取得結果 + API 生JSON を Issue にコメントする
#
# ⚠ 注意:
#   - リポジトリ作成などは一切しない（Actions の GITHUB_TOKEN では権限が足りないため）
#   - コメント本文にはサービス名を出さない

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional, Tuple

import requests
from github import Github, GithubException


# ─────────────────────────────────────────
#  Issue 本文のパース
# ─────────────────────────────────────────

ARTIST_TITLE_RE = re.compile(r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$")


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Issue 本文から (artist, title, video_id) をざっくり取り出す。

    想定フォーマット:
      1行目: 「アーティスト - 曲名」
      どこか: YouTube URL または 「動画ID: xxxxxxxx」
    """
    artist: Optional[str] = None
    title: Optional[str] = None
    video_id: Optional[str] = None

    # 1) 1行目の「artist - title」
    lines = [l.strip() for l in (body or "").splitlines()]
    first_non_empty = next((l for l in lines if l), "")
    m = ARTIST_TITLE_RE.match(first_non_empty)
    if m:
        artist = m.group("artist").strip()
        title = m.group("title").strip()

    # 2) YouTube URL から動画IDを抜く
    yt_pattern = re.compile(
        r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)(?P<vid>[0-9A-Za-z_-]{8,})"
    )
    m2 = yt_pattern.search(body or "")
    if m2:
        video_id = m2.group("vid")
    else:
        # 3) 「動画ID: xxxxxxxx」形式があれば拾う
        vid_pattern = re.compile(
            r"動画ID[^0-9A-Za-z_-]*([0-9A-Za-z_-]{8,})", re.IGNORECASE
        )
        m3 = vid_pattern.search(body or "")
        if m3:
            video_id = m3.group(1)

    return artist or None, title or None, video_id or None


# ─────────────────────────────────────────
#  外部歌詞 API (LrcLib) ラッパー
# ─────────────────────────────────────────

LRC_API_BASE = "https://lrclib.net"


def lrclib_search(track_name: str, artist_name: Optional[str] = None) -> Optional[dict]:
    """
    外部歌詞API (LrcLib) に対して /api/search を実行し、最も良さそうな1件を返す。

    ※ コメント本文にはサービス名は出さないので、あくまで内部的な呼び出し。
    """
    if not track_name:
        return None

    params = {"track_name": track_name}
    if artist_name:
        params["artist_name"] = artist_name

    url = f"{LRC_API_BASE}/api/search"

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[lrclib] search error: {e}", file=sys.stderr)
        return None

    if not isinstance(data, list) or not data:
        return None

    # シンプルに「トラック名が一番近そうなもの or 先頭」を返す
    # （厳密マッチなど欲しくなったらここで工夫する）
    track_lower = track_name.strip().lower()
    best = data[0]

    for rec in data:
        tn = (rec.get("trackName") or "").strip().lower()
        if tn == track_lower:
            best = rec
            break

    return best


def classify_status_from_record(rec: Optional[dict]) -> str:
    """
    取得したレコードから、ステータス文字列を決定する。
    - 同期歌詞あり: Auto/同期あり
    - プレーン歌詞のみ: Auto/同期なし
    - 何もない: 歌詞の登録なし
    """
    if not rec:
        return "歌詞の登録なし"

    plain = (rec.get("plainLyrics") or "").strip()
    synced = (rec.get("syncedLyrics") or "").strip()

    if synced:
        return "Auto/同期あり"
    if plain:
        return "Auto/同期なし"
    return "歌詞の登録なし"


# ─────────────────────────────────────────
#  コメント本文の生成
# ─────────────────────────────────────────

def build_comment_body(
    *,
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    status: str,
    rec: Optional[dict],
) -> str:
    """
    Issue に投稿するコメント本文を生成する。
    ※ サービス名は出さず、「外部歌詞データベース」とだけ書く。
    """
    a = artist or "(不明)"
    t = title or "(不明)"
    v = video_id or "(不明)"

    # 取得元・メッセージ
    source_label = "外部歌詞データベース"
    if rec:
        src_message = (
            f"{source_label} から歌詞情報を取得しました。"
        )
    else:
        src_message = (
            f"{source_label} から該当する歌詞情報を見つけることができませんでした。"
        )

    # レコードから見やすいサマリ
    api_track = (rec or {}).get("trackName") or t
    api_artist = (rec or {}).get("artistName") or a

    # API 生JSON（参考用）
    rec_json = json.dumps(rec or {}, ensure_ascii=False, indent=2)

    lines: list[str] = []
    lines.append("自動歌詞登録の結果をお知らせします 🤖")
    lines.append("")
    lines.append("解析結果")
    lines.append(f"アーティスト: {a}")
    lines.append(f"楽曲名: {t}")
    lines.append(f"動画 ID: {v}")
    lines.append("")
    lines.append("歌詞登録結果")
    lines.append(f"ステータス: {status}")
    lines.append(f"取得元: {source_label}")
    if rec:
        lines.append(
            f"{src_message}（track='{api_track}', artist='{api_artist}'）"
        )
    else:
        lines.append(src_message)

    lines.append("")
    lines.append("取得したデータ（参考・API のそのままの内容）")
    lines.append("```json")
    lines.append(rec_json)
    lines.append("```")
    lines.append("")
    lines.append("※ このコメントは GitHub Actions の自動処理で追加されています。")
    lines.append("※ フォーマット不備などでうまく登録できない場合があります。")

    return "\n".join(lines)


# ─────────────────────────────────────────
#  GitHub へのコメント投稿
# ─────────────────────────────────────────

def post_comment_to_issue(issue_number: int, body: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPOSITORY")

    if not token or not repo_full:
        print("[error] GITHUB_TOKEN / GITHUB_REPOSITORY が設定されていません", file=sys.stderr)
        sys.exit(1)

    gh = Github(token)
    try:
        repo = gh.get_repo(repo_full)  # 例: "neiron-discord/LyricsAddRequest"
    except GithubException as e:
        print(f"[GitHub] get_repo error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        issue = repo.get_issue(number=issue_number)
        issue.create_comment(body)
        print(f"[GitHub] commented to issue #{issue_number}")
    except GithubException as e:
        print(f"[GitHub] create_comment error: {e}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────

def main() -> None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        print("[error] GITHUB_EVENT_PATH が見つかりません", file=sys.stderr)
        sys.exit(1)

    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)

    action = event.get("action")
    issue = event.get("issue")

    if not issue:
        print("[info] issue イベントではないため終了します", file=sys.stderr)
        return

    issue_number = issue.get("number")
    body = issue.get("body") or ""

    print(f"[debug] action={action}, issue_number={issue_number}")

    # Issue 本文を解析
    artist, title, video_id = parse_issue_body(body)
    print(f"[debug] parsed: artist={artist}, title={title}, video_id={video_id}")

    if not title:
        # 曲名が取れないと検索できないので、その旨だけコメントして終了
        comment = (
            "自動歌詞登録の結果をお知らせします 🤖\n\n"
            "Issue 本文から楽曲タイトルを正しく取得できなかったため、自動処理をスキップしました。\n"
            "フォーマット例:\n"
            "  YOASOBI - 夜に駆ける\n"
            "  https://www.youtube.com/watch?v=by4SYYWlhEs\n"
        )
        post_comment_to_issue(issue_number, comment)
        return

    # 外部歌詞API から検索
    rec = lrclib_search(track_name=title, artist_name=artist)
    status = classify_status_from_record(rec)

    comment_body = build_comment_body(
        artist=artist,
        title=title,
        video_id=video_id,
        status=status,
        rec=rec,
    )

    post_comment_to_issue(issue_number, comment_body)


if __name__ == "__main__":
    main()
