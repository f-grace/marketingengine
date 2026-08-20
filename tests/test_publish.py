"""Upload-Post publishing.

The guardrails carry the weight here. Sending a deck to drafts is reversible;
publishing to a live account is not, and on a new account a bad post costs
distribution permanently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import db, publish


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def slides(tmp_path):
    paths = []
    for i in range(1, 7):
        p = tmp_path / f"slide-{i:02d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        paths.append(p)
    return paths


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-key")
    monkeypatch.setenv("UPLOAD_POST_USER", "lynkoai")


class TestDirectPostGuard:
    def test_direct_refused_without_explicit_opt_in(self, slides):
        with pytest.raises(publish.PublishError, match="Refusing DIRECT_POST"):
            publish.upload_deck(slides, "caption", mode=publish.DIRECT)

    def test_error_explains_why_it_is_irreversible(self, slides):
        with pytest.raises(publish.PublishError) as exc:
            publish.upload_deck(slides, "caption", mode=publish.DIRECT)
        assert "irreversible" in str(exc.value)

    def test_draft_is_the_default_mode(self):
        import inspect
        sig = inspect.signature(publish.upload_deck)
        assert sig.parameters["mode"].default == publish.DRAFT


class TestPreflight:
    def test_empty_deck_refused(self):
        with pytest.raises(publish.PublishError, match="No slides"):
            publish.upload_deck([], "caption")

    def test_over_ten_slides_refused(self, tmp_path):
        many = []
        for i in range(11):
            p = tmp_path / f"s{i}.png"
            p.write_bytes(b"x")
            many.append(p)
        with pytest.raises(publish.PublishError, match="10-image limit"):
            publish.upload_deck(many, "caption")

    def test_missing_file_refused_before_upload(self, tmp_path):
        with pytest.raises(publish.PublishError, match="Missing rendered slides"):
            publish.upload_deck([tmp_path / "nope.png"], "caption")


class TestCaption:
    def test_hashtags_appended_to_caption(self):
        idea = {"caption_draft": "Top 5 salaries.",
                "hashtags_json": json.dumps(["finance", "wallstreet"])}
        out = publish.caption_for(idea)
        assert out.startswith("Top 5 salaries.")
        assert "#finance" in out and "#wallstreet" in out

    def test_existing_hash_prefix_not_doubled(self):
        idea = {"caption_draft": "x", "hashtags_json": json.dumps(["#finance"])}
        assert "##" not in publish.caption_for(idea)

    def test_no_hashtags_is_fine(self):
        assert publish.caption_for({"caption_draft": "Just this"}) == "Just this"

    def test_empty_idea_does_not_crash(self):
        assert publish.caption_for({}) == ""


class TestHandoff:
    def test_records_and_marks_status(self, conn):
        conn.execute("INSERT INTO ideas (id, created_at, concept, status) "
                     "VALUES ('u1', 'now', 'c', 'approved')")
        conn.commit()
        publish.record_handoff(conn, "u1", "ext-123")

        assert publish.already_handed_off(conn, "u1")
        row = conn.execute("SELECT status FROM ideas WHERE id='u1'").fetchone()
        assert row["status"] == "handed_off"
        ref = conn.execute("SELECT external_ref FROM handoffs WHERE idea_id='u1'").fetchone()
        assert ref["external_ref"] == "ext-123"

    def test_unsent_idea_is_not_handed_off(self, conn):
        assert not publish.already_handed_off(conn, "nope")

    def test_resend_replaces_rather_than_duplicating(self, conn):
        conn.execute("INSERT INTO ideas (id, created_at, concept, status) "
                     "VALUES ('u1', 'now', 'c', 'approved')")
        conn.commit()
        publish.record_handoff(conn, "u1", "a")
        publish.record_handoff(conn, "u1", "b")
        n = conn.execute("SELECT COUNT(*) FROM handoffs WHERE idea_id='u1'").fetchone()[0]
        assert n == 1

    def test_null_external_ref_allowed(self, conn):
        conn.execute("INSERT INTO ideas (id, created_at, concept, status) "
                     "VALUES ('u1', 'now', 'c', 'approved')")
        conn.commit()
        publish.record_handoff(conn, "u1", None)
        assert publish.already_handed_off(conn, "u1")


class TestConfigErrors:
    def test_missing_key_message_is_actionable(self, monkeypatch):
        monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
        monkeypatch.setattr(publish, "load_env_file", lambda *a, **k: None)
        with pytest.raises(publish.PublishError) as exc:
            publish.api_key()
        msg = str(exc.value)
        assert "upload-post.com" in msg
        assert "paid Upload-Post plan" in msg

    def test_missing_user_message_is_actionable(self, monkeypatch):
        monkeypatch.delenv("UPLOAD_POST_USER", raising=False)
        monkeypatch.setattr(publish, "load_env_file", lambda *a, **k: None)
        with pytest.raises(publish.PublishError, match="UPLOAD_POST_USER"):
            publish.profile_user()


class TestRequestShape:
    def test_sends_expected_fields(self, slides, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "{}"
            def json(self):
                return {"id": "abc123"}

        def fake_post(url, headers=None, data=None, files=None, timeout=None):
            captured.update(url=url, headers=headers, data=data, files=files)
            return FakeResponse()

        monkeypatch.setattr(publish.requests, "post", fake_post)
        body = publish.upload_deck(slides, "my caption")

        assert body["id"] == "abc123"
        assert captured["url"] == publish.ENDPOINT
        assert captured["headers"]["Authorization"] == "Apikey test-key"
        assert captured["data"]["post_mode"] == publish.DRAFT
        assert captured["data"]["platform[]"] == "tiktok"
        assert captured["data"]["title"] == "my caption"
        assert captured["data"]["is_aigc"] == "false"
        assert len(captured["files"]) == 6

    def test_http_error_surfaces_the_body(self, slides, monkeypatch):
        class FakeResponse:
            status_code = 402
            text = "payment required: TikTok needs a paid plan"
            def json(self):
                return {}

        monkeypatch.setattr(publish.requests, "post",
                            lambda *a, **k: FakeResponse())
        with pytest.raises(publish.PublishError, match="402"):
            publish.upload_deck(slides, "c")

    def test_network_failure_is_wrapped(self, slides, monkeypatch):
        def boom(*a, **k):
            raise publish.requests.RequestException("connection reset")
        monkeypatch.setattr(publish.requests, "post", boom)
        with pytest.raises(publish.PublishError, match="connection reset"):
            publish.upload_deck(slides, "c")
