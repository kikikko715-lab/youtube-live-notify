import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import calendar_sync as sync
import monitor

VIDEO = {"videoId": "DlueIewDhy0", "title": "会見", "channelTitle": "参政党【公式】", "scheduledStartTime": "2026-09-11T08:00:00Z"}

class SyncTests(unittest.TestCase):
    def test_event_is_jst_url_and_base32hex_id(self):
        body = sync.event_body(VIDEO)
        self.assertEqual(body["start"]["dateTime"], "2026-09-11T17:00:00+09:00")
        self.assertEqual(body["end"]["dateTime"], "2026-09-11T18:00:00+09:00")
        self.assertRegex(body["id"], r"^[0-9a-v]{5,1024}$")
        self.assertIn(VIDEO["videoId"], body["location"])
        self.assertNotIn("attendees", body)
        self.assertNotIn("conferenceData", body)

    def test_naive_time_and_bad_id_rejected(self):
        for override in ({"scheduledStartTime": "2026-09-11T17:00:00"}, {"videoId": "../evil"}):
            with self.assertRaises(ValueError): sync.event_body(dict(VIDEO, **override))

    def test_merge_preserves_user_notes_properties_and_noop(self):
        original = sync.event_body(VIDEO)
        original["description"] = "自分のメモ\n" + original["description"] + "\n追記"
        original["extendedProperties"]["private"]["personal"] = "keep"
        original["colorId"] = "7"
        wanted = sync.event_body(dict(VIDEO, title="会見の変更", scheduledStartTime="2026-09-12T08:00:00Z"))
        patch_body = sync.merge_patch(original, wanted)
        self.assertTrue(patch_body["description"].startswith("自分のメモ\n"))
        self.assertTrue(patch_body["description"].endswith("\n追記"))
        self.assertNotIn("colorId", patch_body)
        updated = dict(original, **patch_body)
        self.assertEqual(updated["extendedProperties"]["private"]["personal"], "keep")
        self.assertEqual(sync.merge_patch(updated, wanted), {})

    def test_legacy_lookup_adopts_without_create(self):
        calendar = sync.Calendar(Mock(), calendar_id="test-calendar")
        legacy = {"id": "existing-migrated-event", "etag": "v1", "location": "https://www.youtube.com/watch?v=" + VIDEO["videoId"], "description": "自動登録：YouTube通知→Googleカレンダー\n元メールとメモ"}
        calendar.get = Mock(return_value=None)
        calendar.request = Mock(side_effect=[{"items": [legacy]}, {}])
        self.assertEqual(calendar.sync(VIDEO), "updated")
        self.assertEqual(calendar.request.call_args.args[0], "PATCH")
        self.assertEqual(calendar.request.call_args.kwargs["headers"], {"If-Match": "v1"})
        self.assertIn("元メールとメモ", calendar.request.call_args.kwargs["json"]["description"])

    def test_existing_user_event_is_not_modified(self):
        calendar = sync.Calendar(Mock(), calendar_id="test-calendar")
        calendar.find = Mock(return_value={"location": "https://www.youtube.com/watch?v=" + VIDEO["videoId"]})
        calendar.request = Mock()
        self.assertEqual(calendar.sync(VIDEO), "existing-user-event")
        calendar.request.assert_not_called()

    def test_list_search_pages_and_checks_exact_id(self):
        calendar = sync.Calendar(Mock(), calendar_id="test-calendar")
        calendar.get = Mock(return_value=None)
        match = {"location": "https://www.youtube.com/watch?v=" + VIDEO["videoId"]}
        calendar.request = Mock(side_effect=[{"items": [{"description": "wrong"}], "nextPageToken": "next"}, {"items": [match]}])
        self.assertEqual(calendar.find(VIDEO["videoId"]), match)
        self.assertEqual(calendar.request.call_args.kwargs["params"]["pageToken"], "next")

    def test_conflict_after_timeout_does_not_duplicate(self):
        calendar = sync.Calendar(Mock(), calendar_id="test-calendar")
        calendar.find = Mock(return_value=None)
        existing = dict(sync.event_body(VIDEO), etag="v1")
        calendar.request = Mock(return_value=None)  # HTTP 409
        calendar.get = Mock(return_value=existing)
        self.assertEqual(calendar.sync(VIDEO), "unchanged")
        self.assertEqual(calendar.request.call_count, 1)

    def test_pending_retry_after_video_has_started(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pending.json")
            calendar = Mock()
            calendar.sync.side_effect = RuntimeError("offline")
            self.assertEqual(sync.sync_pending([VIDEO], path, calendar), 1)
            self.assertIn(VIDEO["videoId"], sync.read_json(path, {}))
            calendar.sync.side_effect = None
            calendar.sync.return_value = "created"
            self.assertEqual(sync.sync_pending([], path, calendar), 0)
            self.assertEqual(sync.read_json(path, {}), {})

    def test_preview_never_sends_or_syncs(self):
        with patch.dict(os.environ, {"PREVIEW_ONLY": "true"}), patch.object(monitor, "fetch_upcoming", return_value=[VIDEO]), patch.object(monitor, "load_notified", return_value=set()), patch.object(monitor, "send_email") as email, patch.object(monitor, "sync_pending") as calendar:
            monitor.main()
            email.assert_not_called()
            calendar.assert_not_called()

    def test_calendar_failure_does_not_block_email(self):
        with patch.dict(os.environ, {"PREVIEW_ONLY": "false"}), patch.object(monitor, "fetch_upcoming", return_value=[VIDEO]), patch.object(monitor, "load_notified", return_value=set()), patch.object(monitor, "save_notified") as save, patch.object(monitor, "send_email") as email, patch.object(monitor, "sync_pending", return_value=1):
            with self.assertRaises(RuntimeError): monitor.main()
            email.assert_called_once_with([VIDEO])
            save.assert_called_once_with({VIDEO["videoId"]})

    def test_youtube_failure_still_retries_pending(self):
        with patch.dict(os.environ, {"PREVIEW_ONLY": "false"}), patch.object(monitor, "fetch_upcoming", side_effect=RuntimeError("offline")), patch.object(monitor, "load_notified", return_value=set()), patch.object(monitor, "send_email") as email, patch.object(monitor, "sync_pending", return_value=0) as calendar:
            with self.assertRaises(RuntimeError): monitor.main()
            calendar.assert_called_once_with([])
            email.assert_not_called()

if __name__ == "__main__":
    unittest.main()

