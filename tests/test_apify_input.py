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
