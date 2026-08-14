"""
YouTube 配信予定 通知 (GitHub Actions 用)

- 指定チャンネルの「配信予定(upcoming)」をチェック
- 新しい予定を見つけたら Gmail(SMTP)で自分に通知
- state/notified.json に通知済みIDを記録し、二重通知しない

環境変数(GitHubのSecretsで設定):
  YOUTUBE_API_KEY     : YouTube Data API のキー
  GMAIL_ADDRESS       : 送信元=送信先のGmailアドレス
  GMAIL_APP_PASSWORD  : Gmailのアプリパスワード(16桁)
"""

import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

# ===== 監視対象(必要なら書き換え) =====
CHANNEL_IDS = [
    "UCjrN-o1HlLk22qcauIKDtlQ",  # 参政党【公式】
]
STATE_FILE = "state/notified.json"
# =======================================

API_KEY = os.environ["YOUTUBE_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_upcoming():
    results = []
    for ch in CHANNEL_IDS:
        # 1) 配信予定を検索
        params = urllib.parse.urlencode({
            "part": "snippet", "type": "video", "eventType": "upcoming",
            "maxResults": "10", "order": "date", "channelId": ch, "key": API_KEY,
        })
        search = get_json("https://www.googleapis.com/youtube/v3/search?" + params)
        ids = [it["id"]["videoId"] for it in search.get("items", [])
               if it.get("id", {}).get("videoId")]
        if not ids:
            continue

        # 2) 詳細(開始予定時刻)を取得
        vparams = urllib.parse.urlencode({
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(ids), "key": API_KEY,
        })
        videos = get_json("https://www.googleapis.com/youtube/v3/videos?" + vparams)
        for item in videos.get("items", []):
            live = item.get("liveStreamingDetails", {}) or {}
            if not live.get("scheduledStartTime"):
                continue  # 開始予定が無いものは除外
            if live.get("actualStartTime"):
                continue  # すでに開始済みは除外
            results.append({
                "videoId": item["id"],
                "title": item["snippet"]["title"],
                "channelTitle": item["snippet"]["channelTitle"],
                "scheduledStartTime": live["scheduledStartTime"],
                "url": "https://www.youtube.com/watch?v=" + item["id"],
            })
    return results


def load_notified():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_notified(ids):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def fmt_jst(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Tokyo"))
        return dt.strftime("%Y/%m/%d %H:%M JST")
    except Exception:
        return iso


def send_email(fresh):
    lines = []
    for v in fresh:
        lines.append("■ " + v["title"])
        lines.append("  チャンネル: " + v["channelTitle"])
        lines.append("  開始予定  : " + fmt_jst(v["scheduledStartTime"]))
        lines.append("  URL       : " + v["url"])
        lines.append("")
    body = "\n".join(lines) + "\n---\nYouTube配信予定 通知 (GitHub Actions) より自動送信"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(f"【配信予定】{len(fresh)}件の新しいライブ予定", "utf-8"))
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def main():
    notified = load_notified()
    upcoming = fetch_upcoming()
    fresh = [v for v in upcoming if v["videoId"] not in notified]
    print(f"upcoming={len(upcoming)} fresh={len(fresh)}")

    if not fresh:
        print("新しい配信予定はありませんでした。")
        return

    send_email(fresh)
    for v in fresh:
        notified.add(v["videoId"])
    save_notified(notified)
    print(f"通知しました: {len(fresh)}件")


if __name__ == "__main__":
    main()
