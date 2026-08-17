"""Seed content briefs derived from the first live analysis batch.

These were produced by hand from the scraped data while the automated analyze
step waits on an LLM key. They follow exactly the shape `engine/generate.py`
will emit, so they double as the fixture the generator gets checked against.

Every idea traces to a real winner and, where possible, a real comment.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import briefs, config, db  # noqa: E402
from engine.briefs import Idea, Slide  # noqa: E402


def _post_id(conn, handle: str, plays: int):
    row = conn.execute(
        "SELECT p.id FROM posts p JOIN accounts a ON a.id = p.account_id "
        "WHERE a.handle = ? AND p.play_count = ?",
        (handle, plays),
    ).fetchone()
    return row["id"] if row else None


def build(conn):
    fs_law = _post_id(conn, "firmscope.co", 32000)      # 16.4x, 2,299 saves
    fs_bank = _post_id(conn, "firmscope.co", 7518)      # 3.8x, boutique NY banks
    yin_top = _post_id(conn, "youth.investing.network", 2800000)  # 2.8M, reach_only

    ideas = [
        Idea(
            concept="The jobs are not on Indeed",
            hook="7,000 people asked if these finance jobs are on Indeed. They are not.",
            rationale=(
                "The single highest-liked comment in the entire scraped set (7,406 likes) "
                "is 'Are these jobs on indeed' on a 2.8M-view post listing high-paying "
                "finance roles. The audience is telling us, unprompted and at scale, that "
                "they do not know where these roles come from. The honest answer is that "
                "they are filled before they are ever posted, which is the exact gap Lynko "
                "closes.\n\n"
                "Structurally this borrows FirmScope's winning mechanism: name a crowded "
                "default, reveal a specific overlooked number, then hand over the method. "
                "That mechanism produced 16.4x and 17.5x reach on their account with 6-7% "
                "save rates, the highest in the cohort. Here it is grounded in a question "
                "the audience already asked rather than one we invented."
            ),
            slides=[
                Slide(1, "HOOK", "7,000 people asked if these finance jobs are on Indeed.",
                      "Quote the real comment. Screenshot it if possible."),
                Slide(2, "TURN", "They are not. Most were filled before anyone posted them.",
                      "The reveal. Keep it flat, no exclamation."),
                Slide(3, "TENSION", "By the time a role is public, it has 400 applicants.",
                      "Concrete number beats 'a lot'."),
                Slide(4, "PAYOFF", "The roles you want get filled from someone's inbox.",
                      "SAVE TRIGGER. Lands on slide 4 of 7, matching the winners."),
                Slide(5, "PROOF", "That is why students who network get interviews others never see.",
                      "No stats we cannot back."),
                Slide(6, "HOWTO", "Pick 20 firms. Find one person at each. Email before the posting.",
                      "The whole method in one line."),
                Slide(7, "CTA", "Lynko does the finding and the emailing. Link in bio.",
                      "Product mentioned once, after the value landed."),
            ],
            caption=(
                "The top comment on a video about high-paying finance jobs was "
                "\"are these on indeed\". They are not, and that is the whole problem. "
                "Here is where they actually come from."
            ),
            hashtags=["financecareers", "investmentbanking", "internships",
                      "financestudents", "springweek"],
            source_post_ids=[p for p in (yin_top, fs_law) if p],
        ),
        Idea(
            concept="The inner circle is just people who emailed first",
            hook="\"Only the chosen ones get these jobs.\" The chosen ones sent emails.",
            rationale=(
                "Directly answers the strongest objection in the scraped comments: 'Too bad "
                "only the chosen ones get these jobs if you're part of their inner circle, "
                "if not, good luck.' That is the belief standing between this audience and "
                "the product, stated in their own words.\n\n"
                "Objection-reversal is a distinct mechanism from FirmScope's hidden-list "
                "structure, so this diversifies the format mix rather than repeating one "
                "pattern. Reframing defeat as a solvable mechanic is the angle; the risk is "
                "sounding dismissive of a real structural advantage, so slide 3 concedes the "
                "point before turning it."
            ),
            slides=[
                Slide(1, "HOOK", "\"Only the chosen ones get these jobs.\"",
                      "Their words, in quotes. Do not editorialise on slide 1."),
                Slide(2, "TENSION", "This is the most upvoted excuse in finance TikTok.",
                      "Name it without mocking the person."),
                Slide(3, "CONCEDE", "Some of it is true. Legacy and connections are real.",
                      "Concede first or the turn reads as naive."),
                Slide(4, "PAYOFF", "But most of that circle is people who sent an email first.",
                      "SAVE TRIGGER."),
                Slide(5, "PROOF", "A cold email that names one specific thing gets read.",
                      "Mechanism, not a claimed reply rate."),
                Slide(6, "HOWTO", "You need 30 emails, not 300 applications.",
                      "Contrast the effort with what they are already doing."),
                Slide(7, "OBJECTION", "No, it is not spam if you wrote it to one person.",
                      "Pre-empts the objection this post will generate."),
                Slide(8, "CTA", "Lynko writes them from your address. Link in bio.",
                      "Soft. One mention."),
            ],
            caption=(
                "Saw this comment under a finance careers post and it is the most common "
                "belief holding people back. Part of it is true. Most of it is a mechanic "
                "you can copy."
            ),
            hashtags=["financecareers", "networking", "investmentbanking",
                      "internships", "careertips"],
            source_post_ids=[p for p in (yin_top, fs_bank) if p],
        ),
        Idea(
            concept="Your tracker is a list of the most crowded inboxes",
            hook="If your firm list only has names you already knew, so does everyone else's.",
            rationale=(
                "Closest direct adaptation of FirmScope's mechanism, which is the single "
                "most repeatable pattern in the batch: it appears in 4 of 8 winners and "
                "produced the top two reach multiples (16.4x, 17.5x) at 6-7% save rates.\n\n"
                "Worth noting the strategic adjacency: FirmScope sells the list of "
                "uncrowded firms, Lynko sells actually reaching them. Their content does "
                "the market education and stops one step short of our product, so adopting "
                "their mechanism is unusually natural rather than derivative."
            ),
            slides=[
                Slide(1, "HOOK", "If your firm list only has names you already knew, so does everyone else's.",
                      "Longest hook of the three. Test against a shorter cut."),
                Slide(2, "TENSION", "Goldman, JPM, McKinsey. Same 20 names on every tracker.",
                      "Specific names make it concrete."),
                Slide(3, "TURN", "Those inboxes get thousands of identical emails a season.",
                      ""),
                Slide(4, "PAYOFF", "The firms with quiet inboxes are the ones hiring by email.",
                      "SAVE TRIGGER."),
                Slide(5, "PROOF", "Smaller firms have no campus pipeline. They hire who reaches out.",
                      "Mechanism, verifiable."),
                Slide(6, "HOWTO", "Swap 5 famous names for 15 you have never heard of.",
                      "Concrete swap, low effort ask."),
                Slide(7, "CTA", "Lynko finds the person and writes the email. Link in bio.",
                      ""),
            ],
            caption=(
                "Every tracker has the same 20 firms on it. That is not a target list, "
                "it is a list of the most competitive inboxes in the world. Here is the swap."
            ),
            hashtags=["investmentbanking", "springweek", "internships",
                      "financecareers", "financestudents"],
            source_post_ids=[p for p in (fs_law, fs_bank) if p],
        ),
    ]
    return ideas


def main() -> int:
    conn = db.connect(config.DB_PATH)
    ideas = build(conn)
    for idea in ideas:
        briefs.save_idea(conn, idea)
        print(f"  {idea.id[:8]}  {idea.concept}")
    print(f"\n{len(ideas)} ideas saved")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
