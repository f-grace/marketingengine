"""Actor input construction.

The date filter is the interesting case: sending an unverified value for a
field whose format is undocumented is a way to fail a paid run for nothing.
"""

from __future__ import annotations

from engine import apify


class TestPassA:
    def test_omits_date_filter_when_unset(self):
        payload = apify.pass_a_input(["a", "b"], oldest_post_date="")
        assert "oldestPostDateUnified" not in payload

    def test_includes_date_filter_when_set(self):
        payload = apify.pass_a_input(["a"], oldest_post_date="60 days")
        assert payload["oldestPostDateUnified"] == "60 days"

    def test_strips_at_signs_from_handles(self):
        payload = apify.pass_a_input(["@one", "two"])
        assert payload["profiles"] == ["one", "two"]

    def test_no_paid_extras_on_the_wide_sweep(self):
        payload = apify.pass_a_input(["a"])
        for field in ("commentsPerPost", "topLevelCommentsPerPost",
                      "maxRepliesPerComment", "maxFollowersPerProfile"):
            assert payload[field] == 0
        for field in ("shouldDownloadVideos", "shouldDownloadCovers",
                      "shouldDownloadSlideshowImages", "aiVideoDescription",
                      "aiVideoSummary"):
            assert payload[field] is False


class TestPassB:
    def test_enriches_only_named_posts(self):
        payload = apify.pass_b_input(["https://x/1", "https://x/2"])
        assert payload["postURLs"] == ["https://x/1", "https://x/2"]
        assert "profiles" not in payload

    def test_replies_are_excluded(self):
        payload = apify.pass_b_input(["https://x/1"], top_level_comments_per_post=20)
        assert payload["topLevelCommentsPerPost"] == 20
        assert payload["maxRepliesPerComment"] == 0

    def test_downloads_slideshow_images(self):
        payload = apify.pass_b_input(["https://x/1"])
        assert payload["shouldDownloadSlideshowImages"] is True
