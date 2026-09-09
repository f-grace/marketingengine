"""Email digest construction and sending. No sockets are ever opened."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import notify
from engine.prompts import GeneratedPrompt


def _prompt(tmp_path: Path, n: int, with_image: bool = True) -> GeneratedPrompt:
    prompt_path = tmp_path / f"acct-{n}.md"
    prompt_path.write_text(f"# prompt {n}\n")
    image_path = None
    if with_image:
        image_path = tmp_path / f"{n}.jpg"
        image_path.write_bytes(b"\xff\xd8fakejpeg")
    return GeneratedPrompt(
        post_id=n,
        tiktok_id=str(n),
        handle="acct",
        pathway="source_recreate",
        selected_as="winner",
        reach_index=3.5,
        url=f"https://www.tiktok.com/@acct/video/{n}",
        prompt_path=prompt_path,
        image_path=image_path,
    )


class TestBuildDigest:
    def test_subject_and_body(self, tmp_path):
        msg = notify.build_digest(
            [_prompt(tmp_path, 1), _prompt(tmp_path, 2)],
            to="me@example.com", sender="bot@example.com",
        )
        assert "2 new recreation prompts" in msg["Subject"]
        assert msg["To"] == "me@example.com"
        assert msg["From"] == "bot@example.com"
        body = msg.get_body(("plain",)).get_content()
        assert "@acct" in body
        assert "3.5x baseline" in body

    def test_attaches_prompt_and_image(self, tmp_path):
        msg = notify.build_digest([_prompt(tmp_path, 1)],
                                  to="a@b.c", sender="a@b.c")
        names = [p.get_filename() for p in msg.iter_attachments()]
        assert "acct-1.md" in names
        assert "1.jpg" in names

    def test_missing_image_attaches_prompt_only(self, tmp_path):
        msg = notify.build_digest([_prompt(tmp_path, 1, with_image=False)],
                                  to="a@b.c", sender="a@b.c")
        names = [p.get_filename() for p in msg.iter_attachments()]
        assert names == ["acct-1.md"]


class TestBatching:
    def test_small_set_is_one_message(self, tmp_path):
        prompts = [_prompt(tmp_path, i) for i in range(3)]
        assert len(notify.build_digests(prompts, "a@b.c", "a@b.c")) == 1

    def test_large_set_splits(self, tmp_path):
        prompts = [_prompt(tmp_path, i)
                   for i in range(notify.MAX_PROMPTS_PER_MESSAGE + 5)]
        messages = notify.build_digests(prompts, "a@b.c", "a@b.c")
        assert len(messages) == 2
        assert "[1/2]" in messages[0]["Subject"]
        assert "[2/2]" in messages[1]["Subject"]

    def test_batches_cover_every_prompt_once(self, tmp_path):
        prompts = [_prompt(tmp_path, i)
                   for i in range(notify.MAX_PROMPTS_PER_MESSAGE + 5)]
        batches = notify._batches(prompts)
        flat = [p.post_id for batch in batches for p in batch]
        assert flat == [p.post_id for p in prompts]


class TestSend:
    def test_sends_over_ssl_with_login(self, tmp_path, monkeypatch):
        calls = {}

        class FakeSMTP:
            def __init__(self, host, port):
                calls["host"], calls["port"] = host, port

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, address, password):
                calls["login"] = (address, password)

            def send_message(self, msg):
                calls["sent"] = msg["Subject"]

        monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
        msg = notify.build_digest([_prompt(tmp_path, 1)],
                                  to="a@b.c", sender="bot@b.c")
        notify.send(msg, "bot@b.c", "app-password")

        assert calls["host"] == "smtp.gmail.com"
        assert calls["port"] == 465
        assert calls["login"] == ("bot@b.c", "app-password")
        assert "recreation prompts" in calls["sent"]

    def test_failure_propagates_to_caller(self, tmp_path, monkeypatch):
        class BrokenSMTP:
            def __init__(self, *a):
                raise OSError("no route to host")

        monkeypatch.setattr(notify.smtplib, "SMTP_SSL", BrokenSMTP)
        msg = notify.build_digest([_prompt(tmp_path, 1)],
                                  to="a@b.c", sender="bot@b.c")
        with pytest.raises(OSError):
            notify.send(msg, "bot@b.c", "pw")
