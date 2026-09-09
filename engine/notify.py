"""Email delivery of recreation prompts.

Plain SMTP over SSL to Gmail, stdlib only. The credentials are a Gmail App
Password read from the environment (see .env.example); nothing here stores or
logs them.

Delivery is best-effort by design: prompt files are already on disk and their
rows committed before this module runs, and `emailed_at` is stamped only after
a send returns without raising — so a failed send simply leaves the prompts
eligible for the next `python -m engine email`.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import List

from .prompts import GeneratedPrompt

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Gmail rejects messages over 25MB. Cap conservatively and split: attachments
# are one image plus one small .md per prompt.
MAX_PROMPTS_PER_MESSAGE = 20
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _batches(prompts: List[GeneratedPrompt]) -> List[List[GeneratedPrompt]]:
    """Split into messages that stay under Gmail's size limit."""
    batches: List[List[GeneratedPrompt]] = []
    current: List[GeneratedPrompt] = []
    current_bytes = 0
    for p in prompts:
        size = 0
        if p.prompt_path.exists():
            size += p.prompt_path.stat().st_size
        if p.image_path and p.image_path.exists():
            size += p.image_path.stat().st_size
        if current and (len(current) >= MAX_PROMPTS_PER_MESSAGE
                        or current_bytes + size > MAX_ATTACHMENT_BYTES):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(p)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def build_digest(
    prompts: List[GeneratedPrompt], to: str, sender: str,
    part: int = 1, parts: int = 1,
) -> EmailMessage:
    """One digest message: summary body + prompt .md and image attachments.

    The image files are attached deliberately: the CDN URL inside each prompt
    expires within hours, so the attachment is what the user's multimodal LLM
    actually gets to look at.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"Marketing Engine: {len(prompts)} new recreation prompts ({date})"
    if parts > 1:
        subject += f" [{part}/{parts}]"

    lines = ["New recreation prompts. Each has a .md prompt file attached; "
             "paste it into your image LLM together with the matching source "
             "image attachment.", ""]
    for p in prompts:
        reach = (f"{p.reach_index:.1f}x baseline"
                 if p.reach_index is not None else "baseline unknown")
        image = p.image_path.name if p.image_path else "no image downloaded"
        lines.append(f"- @{p.handle} · {p.pathway} · {p.selected_as} · {reach}")
        lines.append(f"    prompt: {p.prompt_path.name}   image: {image}")
        if p.url:
            lines.append(f"    post: {p.url}")
    lines.append("")
    lines.append("Files also on disk under data/exports/prompts/.")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))

    for p in prompts:
        if p.prompt_path.exists():
            msg.add_attachment(
                p.prompt_path.read_bytes(),
                maintype="text", subtype="markdown",
                filename=p.prompt_path.name,
            )
        if p.image_path and p.image_path.exists():
            suffix = p.image_path.suffix.lstrip(".").lower() or "jpeg"
            if suffix == "jpg":
                suffix = "jpeg"
            msg.add_attachment(
                p.image_path.read_bytes(),
                maintype="image", subtype=suffix,
                filename=p.image_path.name,
            )
    return msg


def build_digests(
    prompts: List[GeneratedPrompt], to: str, sender: str
) -> List[EmailMessage]:
    batches = _batches(prompts)
    return [
        build_digest(batch, to, sender, part=i + 1, parts=len(batches))
        for i, batch in enumerate(batches)
    ]


def send(msg: EmailMessage, address: str, app_password: str) -> None:
    """Deliver one message. Raises on failure; the caller decides what that
    means (for the CLI: report, leave `emailed_at` unset, move on)."""
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(address, app_password)
        smtp.send_message(msg)
