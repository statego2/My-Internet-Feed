import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import build

class FeedTests(unittest.TestCase):
    def test_missing_details_keeps_rss_video_without_invented_duration(self):
        now = datetime.now(timezone.utc).isoformat()
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>abcdefghijk</yt:videoId><title>New upload</title><published>{now}</published></entry></feed>'''
        ch = dict(id="channel", title="Test", filter="")
        with patch.object(build, "http", return_value=(200, xml)), patch.object(build, "yt_details", return_value=None):
            items, warning = build.fetch_yt(ch, 14, 6, {})
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0]["dur"])
            self.assertTrue(items[0]["metadata_pending"])
            self.assertTrue(warning)
            ch["filter"] = "minlen:600"
            items, warning = build.fetch_yt(ch, 14, 6, {})
            self.assertIsNone(items)
            self.assertTrue(warning)

    def test_confirmed_shorts_remain_excluded(self):
        now = datetime.now(timezone.utc).isoformat()
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>abcdefghijk</yt:videoId><title>Short</title><published>{now}</published></entry></feed>'''
        with patch.object(build, "http", return_value=(200, xml)), patch.object(build, "yt_details", return_value=dict(len=60, live=False, upcoming=False, short=True)):
            items, warning = build.fetch_yt(dict(id="channel", title="Test", filter=""), 14, 6, {})
            self.assertEqual(items, [])
            self.assertIsNone(warning)
