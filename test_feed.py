import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import build

class FeedTests(unittest.TestCase):
    def fetch(self, details, filt=""):
        now = datetime.now(timezone.utc).isoformat()
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>abcdefghijk</yt:videoId><title>New upload</title><published>{now}</published></entry></feed>'''
        ch = dict(id="channel", title="Test", filter=filt)
        with patch.object(build, "http", return_value=(200, xml)), patch.object(build, "channel_videos", return_value={}), patch.object(build, "yt_details", return_value=details):
            return build.fetch_yt(ch, 14, 6, {})

    def test_unclassified_is_withheld(self):
        items, warning = self.fetch(None)
        self.assertEqual(items, [])
        self.assertIn("withheld", warning)

    def test_verified_regular_without_duration(self):
        details = dict(len=None, live=False, upcoming=False, short=False, classification_verified=True)
        items, warning = self.fetch(details)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["dur"])
        self.assertTrue(items[0]["classification_verified"])
        self.assertEqual(self.fetch(details, "minlen:600")[0], [])

    def test_confirmed_short_excluded(self):
        items, warning = self.fetch(dict(len=60, live=False, upcoming=False, short=True, classification_verified=True))
        self.assertEqual(items, [])

    def test_short_regular_video_kept(self):
        items, warning = self.fetch(dict(len=60, live=False, upcoming=False, short=False, classification_verified=True))
        self.assertEqual(len(items), 1)

    def test_blocked_metadata_is_not_classification(self):
        with patch.object(build, "http", return_value=(0, "")), patch.object(build, "classify_yt", return_value=None):
            self.assertIsNone(build.yt_details("abcdefghijk", {}))

    def test_verified_video_tab_survives_watch_block(self):
        with patch.object(build, "http", return_value=(0, "")):
            self.assertEqual(build.yt_details("abcdefghijk", {}, {"abcdefghijk": 90})["len"], 90)
