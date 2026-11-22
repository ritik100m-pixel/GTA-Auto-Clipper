import unittest
from unittest.mock import patch

from find_channel_videos import find_channel_videos


class FindChannelVideosTests(unittest.TestCase):
    @patch("find_channel_videos._get")
    def test_discovers_recent_regular_uploads_and_excludes_live(self, get):
        get.side_effect = [
            {"items": [{
                "id": "UC-one",
                "snippet": {"title": "Creator"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU-one"}},
            }]},
            {"items": [
                {"snippet": {"title": "Recent", "publishedAt": "2026-11-22T10:00:00Z"},
                 "contentDetails": {"videoId": "vod1"}},
                {"snippet": {"title": "Live", "publishedAt": "2026-11-22T10:00:00Z"},
                 "contentDetails": {"videoId": "live1"}},
            ]},
            {"items": [
                {"id": "vod1", "snippet": {"title": "Recent", "liveBroadcastContent": "none"}},
                {"id": "live1", "snippet": {"title": "Live", "liveBroadcastContent": "live"},
                 "liveStreamingDetails": {"actualStartTime": "2026-11-22T10:00:00Z"}},
            ]},
        ]
        result = find_channel_videos(
            [{"channel_id": "UC-one"}], "test-key",
            now=__import__("datetime").datetime.fromisoformat("2026-11-22T12:00:00+00:00"),
        )
        self.assertEqual([item["video_id"] for item in result], ["vod1"])
        self.assertEqual(result[0]["source_type"], "youtube_video")
        self.assertEqual(get.call_count, 3)

    def test_empty_channel_list_does_not_call_api(self):
        with patch("find_channel_videos._get") as get:
            self.assertEqual(find_channel_videos([], "test-key"), [])
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
