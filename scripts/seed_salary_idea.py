"""Salary-countdown brief, modelled on the highest-reach post in the scrape."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import briefs, config, db          # noqa: E402
from engine.briefs import Idea, Slide          # noqa: E402


def main() -> int:
    conn = db.connect(config.DB_PATH)
    src = conn.execute(
        "SELECT id FROM posts WHERE play_count = 2800000"
    ).fetchone()

    idea = Idea(
        concept="Top 5 highest-paying finance jobs (and where they actually come from)",
        hook="Top 5 highest paying jobs in finance",
        rationale=(
            "Runs the highest-reach mechanism in the entire scrape. "
            "@youth.investing.network did 2.8M plays and 42,232 saves with exactly this "
            "format: a 5-to-1 salary countdown over Wolf of Wall Street stills, six "
            "slides, white outlined text, no box.\n\n"
            "The difference is slide 7. Their post's top comment, at 7,406 likes and the "
            "most-liked comment in the whole dataset, was 'Are these jobs on indeed'. They "
            "never answered it. We run their proven reach format and then land the one "
            "thing the audience explicitly asked for. That turns a pure-reach post into a "
            "reach post with a door at the end.\n\n"
            "Note the mechanism differs from the FirmScope briefs: this is a countdown, so "
            "the payoff is #1 at the END, not slide 4. Do not apply the slide-4 payoff rule "
            "here.\n\n"
            "Expect argument in the comments about the numbers. That is not a failure. The "
            "source post's engagement was driven substantially by people disputing its "
            "figures, and every dispute is a comment."
        ),
        slides=[
            Slide(1, "HOOK", "Top 5 highest paying jobs in finance",
                  "Title card. Wide establishing shot, suited figure, desaturated."),
            Slide(2, "ENTRY 5", "5. Investment Banking Analyst  |  $150,000-$200,000",
                  "First-year total comp, base plus bonus. VERIFY before posting."),
            Slide(3, "ENTRY 4", "4. Private Equity Associate  |  $250,000-$400,000",
                  "Post-banking associate. VERIFY."),
            Slide(4, "ENTRY 3", "3. Venture Capital Partner  |  $400,000-$1,000,000+",
                  "Wide range is honest here, carry varies enormously. VERIFY."),
            Slide(5, "ENTRY 2", "2. Quant Researcher  |  $300,000-$900,000",
                  "Crowd-pleaser. A commenter cited a $400k XTX intern, 256 likes. VERIFY."),
            Slide(6, "ENTRY 1", "1. Hedge Fund PM  |  $1,000,000-$10,000,000+",
                  "The payoff. Biggest number, most dramatic still."),
            Slide(7, "TURN", "None of these were posted on a job board.",
                  "THE DIFFERENTIATOR. Answers the 7,406-like comment they ignored."),
            Slide(8, "CTA", "They get filled from someone's inbox. Lynko in bio.",
                  "One product mention, after the value landed."),
        ],
        caption=(
            "Top 5 highest paying jobs in finance. Numbers are total comp, base plus "
            "bonus, US roles. Argue with me in the comments. And before you ask: no, "
            "none of these were on Indeed."
        ),
        hashtags=["finance", "investmentbanking", "privateequity", "hedgefund",
                  "quantfinance", "financecareers", "financestudents", "wallstreet"],
        source_post_ids=[src["id"]] if src else [],
    )
    briefs.save_idea(conn, idea)
    print(f"  {idea.id[:8]}  {idea.concept}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
