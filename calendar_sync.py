"""Direct Calendar sync from the same GitHub Actions run as the notifier."""
import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

OWNER = "youtube-live-notify"
START = "[YouTube自動登録]"
END = "[/YouTube自動登録]"
JST = timezone(timedelta(hours=9))


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def event_id(video_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video ID")
    # Hex is within the Calendar API's base32hex event-ID alphabet.
    return "c" + hashlib.sha256(video_id.encode()).hexdigest()


def event_body(video):
    vid = video["videoId"]
    eid = event_id(vid)
    when = datetime.fromisoformat(video["scheduledStartTime"].replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("A timezone is required")
    when = when.astimezone(JST)
    url = "https://www.youtube.com/watch?v=" + vid
    description = (
        START + "\n視聴URL：" + url
        + "\nチャンネル：" + html.escape(video["channelTitle"])
        + "\n開始予定：" + when.strftime("%Y/%m/%d %H:%M JST")
        + "\n終了時刻は未定のため、カレンダー上は仮に1時間で登録しています。"
        + "\n自動登録：YouTube通知→Googleカレンダー"
        + "\nYouTube動画ID：" + vid + "\n" + END
    )
    return {
        "id": eid, "summary": video["title"], "location": url,
        "description": description,
        "start": {"dateTime": when.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": (when + timedelta(hours=1)).isoformat(), "timeZone": "Asia/Tokyo"},
        "transparency": "transparent",
        "extendedProperties": {"private": {"managedBy": OWNER, "youtubeVideoId": vid}},
    }


def same_video(event, vid):
    private = event.get("extendedProperties", {}).get("private", {})
    if private.get("youtubeVideoId") == vid:
        return True
    text = html.unescape(event.get("description", "") + " " + event.get("location", ""))
    return re.search(r"(?:[?&]v=|youtu\.be/)" + re.escape(vid) + r"(?![A-Za-z0-9_-])", text) is not None


def managed(event, vid):
    private = event.get("extendedProperties", {}).get("private", {})
    return same_video(event, vid) and (
        private.get("managedBy") == OWNER
        or "自動登録：YouTube通知→Googleカレンダー" in event.get("description", "")
    )


def merge_patch(existing, wanted):
    # Preserve manual notes, reminders, colors, guests and all unrelated properties.
    patch = {k: wanted[k] for k in ("summary", "start", "end", "location") if existing.get(k) != wanted[k]}
    old_description = existing.get("description", "")
    block = wanted["description"]
    pattern = re.escape(START) + r".*?" + re.escape(END)
    if re.search(pattern, old_description, flags=re.S):
        description = re.sub(pattern, lambda _: block, old_description, count=1, flags=re.S)
    else:
        description = old_description + ("\n\n" if old_description else "") + block
    if description != old_description:
        patch["description"] = description
    private = dict(existing.get("extendedProperties", {}).get("private", {}))
    private.update(wanted["extendedProperties"]["private"])
    properties = dict(existing.get("extendedProperties", {}))
    properties["private"] = private
    if properties != existing.get("extendedProperties"):
        patch["extendedProperties"] = properties
    return patch


class Calendar:
    def __init__(self, session, calendar_id=None):
        self.session = session
        calendar_id = calendar_id or os.environ["GOOGLE_CALENDAR_ID"]
        self.base = "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="") + "/events"

    @classmethod
    def connect(cls):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        info = json.loads(os.environ["GOOGLE_CALENDAR_CREDENTIALS"])
        # Only Google's fixed OAuth endpoint may receive the signed credential.
        if info.get("type") != "service_account" or info.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError("Expected a Google service account credential")
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar.events"]
        )
        return cls(AuthorizedSession(credentials))

    def request(self, method, suffix="", allow=(), **kwargs):
        response = self.session.request(method, self.base + suffix, timeout=30, **kwargs)
        if response.status_code in allow:
            return None
        if not 200 <= response.status_code < 300:
            # Avoid printing credentials, response bodies or private event details.
            raise RuntimeError("Calendar API HTTP " + str(response.status_code))
        return response.json()

    def get(self, eid):
        return self.request("GET", "/" + quote(eid, safe=""), allow=(404,))

    def find(self, vid):
        for eid in [event_id(vid)]:
            found = self.get(eid)
            if found:
                if not same_video(found, vid):
                    # A cancelled/deleted ID or unexpected event must not be replaced.
                    raise RuntimeError("Existing event requires review")
                return found
        # Search all dates: rescheduling can move an existing video by weeks.
        token = None
        while True:
            params = {"q": vid, "maxResults": 2500, "showDeleted": "false"}
            if token:
                params["pageToken"] = token
            page = self.request("GET", params=params)
            for event in page.get("items", []):
                if same_video(event, vid):
                    return event
            token = page.get("nextPageToken")
            if not token:
                return None

    def sync(self, video):
        wanted = event_body(video)
        existing = self.find(video["videoId"])
        if existing is None:
            created = self.request("POST", json=wanted, params={"sendUpdates": "none"}, allow=(409,))
            if created is not None:
                return "created"
            # Same deterministic ID may already have been inserted on a timed-out run.
            existing = self.get(wanted["id"])
            if not existing or not same_video(existing, video["videoId"]):
                raise RuntimeError("Event conflict requires review")
        if existing.get("status") == "cancelled":
            return "cancelled"
        if not managed(existing, video["videoId"]):
            return "existing-user-event"
        patch = merge_patch(existing, wanted)
        if not patch:
            return "unchanged"
        if not existing.get("etag"):
            raise RuntimeError("Missing event etag")
        self.request("PATCH", "/" + quote(existing["id"], safe=""),
                     json=patch, params={"sendUpdates": "none"}, headers={"If-Match": existing["etag"]})
        return "updated"


def sync_pending(videos, path="state/calendar-pending.json", calendar=None):
    pending = read_json(path, {})
    for video in videos:
        event_body(video)  # Validate before persisting.
        pending[video["videoId"]] = video
    write_json(path, pending)
    if not pending:
        return 0
    calendar = calendar or Calendar.connect()
    failures = 0
    for vid, video in list(pending.items()):
        try:
            result = calendar.sync(video)
        except Exception:
            failures += 1
            print("Calendar retry pending: " + vid)
        else:
            print("Calendar " + result + ": " + vid)
            del pending[vid]
            write_json(path, pending)
    return failures


