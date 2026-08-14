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

import html
import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

# ===== 監視対象(必要なら書き換え) =====
CHANNEL_IDS = [
    "UCjrN-o1HlLk22qcauIKDtlQ",  # 参政党【公式】
    "UCgL2ASs0dGsAbph0iCZhJcg",  # 参政党 国会質問チャンネル【公式】
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


JP_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def to_jst(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Tokyo"))


def fmt_jst(iso):
    try:
        dt = to_jst(iso)
        return dt.strftime("%Y/%m/%d %H:%M JST")
    except Exception:
        return iso


def countdown_label(dt):
    """開始まで あと何日 かを日本語で返す"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    diff = (dt.date() - today).days
    if diff < 0:
        return "開始済み"
    if diff == 0:
        return "今日"
    if diff == 1:
        return "明日"
    if diff == 2:
        return "明後日"
    return f"{diff}日後"


def build_card(v):
    """1件分のカレンダー風カード(HTML)"""
    dt = to_jst(v["scheduledStartTime"])
    weekday = JP_WEEKDAYS[dt.weekday()]
    cd = countdown_label(dt)
    time_str = dt.strftime("%H:%M")
    title = html.escape(v["title"])
    channel = html.escape(v["channelTitle"])
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="border-collapse:separate;border-spacing:0;margin:0 0 16px 0;'
        'border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;background:#ffffff;">'
        '<tr>'
        '<td width="96" valign="top" style="background:#c62828;color:#ffffff;'
        'text-align:center;padding:16px 8px;">'
        f'<div style="font-size:12px;letter-spacing:1px;">{dt.month}月</div>'
        f'<div style="font-size:36px;font-weight:700;line-height:1.1;">{dt.day}</div>'
        f'<div style="font-size:13px;margin-top:2px;">（{weekday}）</div>'
        '</td>'
        '<td valign="top" style="padding:14px 18px;">'
        '<span style="display:inline-block;background:#fdecea;color:#c62828;'
        'font-size:12px;font-weight:700;padding:3px 12px;border-radius:999px;">'
        f'{cd} ・ {time_str}〜</span>'
        f'<div style="font-size:15px;font-weight:700;margin:10px 0 6px 0;'
        f'color:#111111;line-height:1.4;">{title}</div>'
        f'<div style="font-size:12px;color:#666666;">📺 {channel}</div>'
        f'<a href="{v["url"]}" style="display:inline-block;margin-top:12px;'
        'background:#c62828;color:#ffffff;text-decoration:none;padding:9px 18px;'
        'border-radius:8px;font-size:13px;font-weight:700;">▶ YouTubeで見る</a>'
        '</td></tr></table>'
    )


def send_email(fresh):
    fresh = sorted(fresh, key=lambda v: v["scheduledStartTime"])  # 開始が近い順

    # プレーンテキスト版(HTML非対応クライアント向けのフォールバック)
    lines = []
    for v in fresh:
        lines.append("■ " + v["title"])
        lines.append("  チャンネル: " + v["channelTitle"])
        lines.append("  開始予定  : " + fmt_jst(v["scheduledStartTime"]))
        lines.append("  URL       : " + v["url"])
        lines.append("")
    text_body = "\n".join(lines) + "\n---\nYouTube配信予定 通知 (GitHub Actions) より自動送信"

    # HTML版(カレンダー風)
    cards = "".join(build_card(v) for v in fresh)
    html_body = (
        '<div style="background:#f3f4f6;padding:20px 12px;'
        "font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;\">"
        '<div style="max-width:600px;margin:0 auto;">'
        f'<div style="font-size:18px;font-weight:700;color:#111111;margin-bottom:4px;">'
        f'🔔 新しいライブ配信予定 {len(fresh)}件</div>'
        '<div style="font-size:12px;color:#888888;margin-bottom:16px;">'
        '開始が近い順に並んでいます</div>'
        f'{cards}'
        '<div style="font-size:11px;color:#aaaaaa;text-align:center;margin-top:8px;">'
        'YouTube配信予定 通知 (GitHub Actions) より自動送信</div>'
        '</div></div>'
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(f"【配信予定】{len(fresh)}件の新しいライブ予定", "utf-8"))
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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
