"""Render approved ideas into content briefs.

Two surfaces, because scanning twenty ideas and reading one deeply are
different jobs:

    ideas table ──┬──> Sheet tab      one row per idea, for triage
                  └──> Markdown doc   full slide-by-slide brief, per idea

The brief is what gets handed to the generation system, so it has to be
specific enough to build from without going back to the source: every slide,
the caption, the hashtag set carried from the winner, and the sound.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import db


@dataclass
class Slide:
    index: int
    role: str          # HOOK | TENSION | TURN | PAYOFF | PROOF | HOWTO | OBJECTION | CTA
    text: str
    note: str = ""


@dataclass
class Idea:
    concept: str
    hook: str
    caption: str
    hashtags: List[str]
    slides: List[Slide]
    rationale: str
    source_post_ids: List[int] = field(default_factory=list)
    suggested_music_id: Optional[str] = None
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_idea(conn: sqlite3.Connection, idea: Idea) -> str:
    """Persist an idea. The returned UUID is the handoff join key.

    That id is what must travel into the generation system and come back
    attached to the published post URL, or the Phase 3 feedback loop cannot
    exist. See PLAN.md section 3.
    """
    conn.execute(
        """INSERT OR REPLACE INTO ideas
           (id, created_at, cluster_id, source_analysis_ids_json, concept, hook,
            caption_draft, hashtags_json, slide_outline_json,
            suggested_music_id, rationale, status, edited_by_human)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (
            idea.id, _now(), None, json.dumps(idea.source_post_ids),
            idea.concept, idea.hook, idea.caption,
            json.dumps(idea.hashtags),
            json.dumps([{"slide": s.index, "role": s.role, "text": s.text,
                         "note": s.note} for s in idea.slides]),
            idea.suggested_music_id, idea.rationale, "proposed",
        ),
    )
    conn.commit()
    return idea.id


def _source_block(conn: sqlite3.Connection, post_ids: List[int]) -> str:
    """The provenance trail: what this idea was modelled on and why."""
    if not post_ids:
        return "_No source posts recorded._\n"
    lines = []
    for pid in post_ids:
        row = conn.execute(
            """SELECT a.handle, p.url, p.play_count, p.collect_count,
                      p.slide_count, s.reach_index, s.save_rate, s.selected_as
               FROM posts p JOIN accounts a ON a.id = p.account_id
               LEFT JOIN post_scores s ON s.post_id = p.id
               WHERE p.id = ?""",
            (pid,),
        ).fetchone()
        if not row:
            continue
        lines.append(
            f"- **@{row['handle']}** · {row['play_count']:,} plays · "
            f"{row['collect_count']:,} saves · "
            f"{(row['reach_index'] or 0):.1f}x their baseline · "
            f"{row['slide_count']} slides · `{row['selected_as']}`  \n"
            f"  {row['url']}"
        )
    return "\n".join(lines) + "\n"


def render_markdown(conn: sqlite3.Connection, idea_id: str) -> str:
    """Full slide-by-slide brief for one idea."""
    row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    if not row:
        raise KeyError(f"no idea {idea_id}")

    hashtags = json.loads(row["hashtags_json"] or "[]")
    slides = json.loads(row["slide_outline_json"] or "[]")
    sources = json.loads(row["source_analysis_ids_json"] or "[]")

    out = [
        f"# {row['concept']}",
        "",
        f"`{row['id']}` · status **{row['status']}** · {row['created_at'][:10]}",
        "",
        "> Carry this id into the generation system and return it with the",
        "> published URL, or the performance loop cannot close.",
        "",
        "## Modelled on",
        "",
        _source_block(conn, sources),
        "## Why this should work",
        "",
        row["rationale"] or "",
        "",
        "## Hook (slide 1)",
        "",
        f"**{row['hook']}**",
        "",
        f"## Slides ({len(slides)})",
        "",
        "| # | Role | On-slide text | Note |",
        "|---|------|---------------|------|",
    ]
    for s in slides:
        text = (s.get("text") or "").replace("|", "\\|")
        note = (s.get("note") or "").replace("|", "\\|")
        out.append(f"| {s.get('slide')} | {s.get('role')} | {text} | {note} |")

    out += [
        "",
        "## Caption",
        "",
        row["caption_draft"] or "",
        "",
        "## Hashtags",
        "",
        " ".join(f"#{h}" for h in hashtags),
        "",
    ]
    if row["suggested_music_id"]:
        out += ["## Sound", "", f"`musicId {row['suggested_music_id']}`", ""]

    out += [
        "## Approval",
        "",
        "- [ ] Concept approved",
        "- [ ] Hook approved",
        "- [ ] Slides approved",
        "- [ ] Caption and hashtags approved",
        "",
        "_Edit anything above before handing off. Set status to `approved` or",
        "`rejected` in the ideas table when done._",
        "",
    ]
    return "\n".join(out)


def export_briefs(conn: sqlite3.Connection, out_dir: Path) -> List[Path]:
    """Write one markdown brief per idea."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for row in conn.execute("SELECT id, concept FROM ideas ORDER BY created_at"):
        slug = "".join(
            ch if ch.isalnum() or ch in "-_" else "-"
            for ch in (row["concept"] or "idea").lower()
        )[:60].strip("-")
        path = out_dir / f"{slug}-{row['id'][:8]}.md"
        path.write_text(render_markdown(conn, row["id"]))
        written.append(path)
    return written


def triage_rows(conn: sqlite3.Connection) -> List[Dict]:
    """One row per idea for the Sheet triage tab."""
    rows = []
    for row in conn.execute(
        "SELECT * FROM ideas ORDER BY created_at DESC"
    ):
        slides = json.loads(row["slide_outline_json"] or "[]")
        hashtags = json.loads(row["hashtags_json"] or "[]")
        sources = json.loads(row["source_analysis_ids_json"] or "[]")
        source_handles = []
        for pid in sources:
            r = conn.execute(
                "SELECT a.handle FROM posts p JOIN accounts a ON a.id=p.account_id "
                "WHERE p.id = ?", (pid,),
            ).fetchone()
            if r:
                source_handles.append("@" + r["handle"])
        rows.append({
            "idea_id": row["id"],
            "status": row["status"],
            "concept": row["concept"],
            "hook": row["hook"],
            "slides": len(slides),
            "hashtags": " ".join("#" + h for h in hashtags),
            "modelled_on": ", ".join(sorted(set(source_handles))),
            "created_at": row["created_at"][:16],
        })
    return rows
