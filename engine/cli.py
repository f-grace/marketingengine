"""Command line entry point.

    python -m engine init      create config files and an empty store
    python -m engine scrape    Pass A: wide sweep over every tracked account
    python -m engine score     baselines, scores, and pick the analysis batch
    python -m engine enrich    Pass B: comments + slide images for the batch
    python -m engine export    write CSVs (and Google Sheets if configured)
    python -m engine status    what is in the store right now
    python -m engine run       scrape -> score -> enrich -> export

`scrape` and `enrich` cost money. Both print an estimate and, unless you pass
--yes, ask before calling the actor.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import apify, config, db, export, ingest, pipeline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db() -> sqlite3.Connection:
    return db.connect(config.DB_PATH)


def _budget_warning(estimate: float) -> Optional[str]:
    """Warn when one run eats a serious share of the free monthly credit.

    The free tier is $5/month and a full sweep is $3.70, so it is genuinely easy
    to spend the month's allowance on a single exploratory run.
    """
    credit = apify.FREE_TIER_MONTHLY_CREDIT
    if estimate >= credit:
        return (f"This run alone (~${estimate:.2f}) exceeds the ${credit:.2f} "
                "free monthly credit. Cut the account list or results_per_page "
                "in config/accounts.yml, or make sure you are on a paid tier.")
    if estimate >= credit * 0.5:
        return (f"This run (~${estimate:.2f}) uses "
                f"{estimate / credit * 100:.0f}% of the ${credit:.2f} free "
                "monthly credit. Consider starting with 2-3 accounts and a "
                "lower results_per_page to validate the setup cheaply.")
    return None


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("  not a TTY and --yes was not passed; refusing to spend money.")
        return False
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in {"y", "yes"}


# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    pairs = [
        (config.CONFIG_DIR / "accounts.example.yml", config.CONFIG_DIR / "accounts.yml"),
        (config.CONFIG_DIR / "brand.example.json", config.CONFIG_DIR / "brand.json"),
        (config.ROOT / ".env.example", config.ROOT / ".env"),
    ]
    for src, dst in pairs:
        if not src.exists():
            print(f"  missing template {src.name}, skipping")
            continue
        if dst.exists():
            print(f"  {dst.name} already exists, left alone")
            continue
        shutil.copy(src, dst)
        print(f"  created {dst.relative_to(config.ROOT)}")

    conn = _open_db()
    conn.close()
    print(f"  store ready at {config.DB_PATH.relative_to(config.ROOT)}")
    print("\nNext:")
    print("  1. Put your Apify token in .env")
    print("  2. List your handle and competitors in config/accounts.yml")
    print("  3. python -m engine scrape")
    return 0


def cmd_scrape(args) -> int:
    accounts = config.load_accounts()
    settings = config.load_scrape_settings()
    token = config.apify_token()

    handles = accounts.all_handles
    est = len(handles) * settings.results_per_page / 1000.0 * apify.USD_PER_1000_RESULTS

    print(f"Pass A over {len(handles)} accounts "
          f"x {settings.results_per_page} posts")
    print(f"  own: {', '.join(accounts.own) or '(none set)'}")
    print(f"  estimated cost: ~${est:.2f} (directional, not a quote)")

    warning = _budget_warning(est)
    if warning:
        print(f"  BUDGET: {warning}")

    if not accounts.own:
        print("  NOTE: no 'own' handle configured. Add it now so baseline "
              "history starts accumulating; you cannot backfill it later.")

    if not _confirm("Run the actor?", args.yes):
        print("Aborted.")
        return 1

    run_input = apify.pass_a_input(
        handles,
        results_per_page=settings.results_per_page,
        oldest_post_date=settings.oldest_post_date,
        proxy_country_code=settings.proxy_country_code,
    )

    conn = _open_db()
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, pass_name, actor_input_json) "
        "VALUES (?, 'A', ?)",
        (_now(), str(run_input)),
    )
    run_row_id = cur.lastrowid
    conn.commit()

    try:
        result = apify.run_actor(token, run_input, raw_dir=config.RAW_DIR, pass_name="A")
    except apify.ApifyError as exc:
        conn.execute("UPDATE scrape_runs SET error = ?, finished_at = ? WHERE id = ?",
                     (str(exc), _now(), run_row_id))
        conn.commit()
        print(f"ERROR: {exc}")
        return 1

    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, result_count = ?, "
        "raw_path = ?, apify_run_id = ? WHERE id = ?",
        (_now(), len(result.items), str(result.raw_path), result.run_id, run_row_id),
    )
    conn.commit()

    print(f"  {len(result.items)} results, ~${result.estimated_cost_usd:.2f}")
    print(f"  raw dataset saved to {result.raw_path}")

    report = ingest.ingest_items(conn, result.items, accounts.own, run_row_id)
    print(f"  ingested: {report.summary()}")
    conn.close()
    return 0


def cmd_score(args) -> int:
    settings = config.load_scrape_settings()
    conn = _open_db()
    report = pipeline.score_and_select(
        conn,
        n_winners=settings.n_winners,
        n_losers=settings.n_losers,
        n_anomalies=settings.n_anomalies,
        n_reach_only=settings.n_reach_only,
    )
    print("Scoring complete:")
    print(report.summary())
    if report.cohort_size == 0:
        print("\n  No carousel posts found. Either the tracked accounts do not "
              "post slideshows, or nothing has been scraped yet.")
    conn.close()
    return 0


def cmd_enrich(args) -> int:
    settings = config.load_scrape_settings()
    accounts = config.load_accounts()
    token = config.apify_token()

    conn = _open_db()
    urls = pipeline.selected_post_urls(conn)
    if not urls:
        print("Nothing selected. Run `python -m engine score` first.")
        conn.close()
        return 1

    est = (len(urls) * (1 + settings.top_level_comments_per_post) / 1000.0
           * apify.USD_PER_1000_RESULTS)
    print(f"Pass B over {len(urls)} selected posts "
          f"+ {settings.top_level_comments_per_post} comments each")
    print(f"  estimated cost: ~${est:.2f} (directional, not a quote)")

    warning = _budget_warning(est)
    if warning:
        print(f"  BUDGET: {warning}")

    if not _confirm("Run the actor?", args.yes):
        print("Aborted.")
        conn.close()
        return 1

    run_input = apify.pass_b_input(
        urls,
        top_level_comments_per_post=settings.top_level_comments_per_post,
        proxy_country_code=settings.proxy_country_code,
    )
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, pass_name, actor_input_json) "
        "VALUES (?, 'B', ?)",
        (_now(), str(run_input)),
    )
    run_row_id = cur.lastrowid
    conn.commit()

    try:
        result = apify.run_actor(token, run_input, raw_dir=config.RAW_DIR, pass_name="B")
    except apify.ApifyError as exc:
        conn.execute("UPDATE scrape_runs SET error = ?, finished_at = ? WHERE id = ?",
                     (str(exc), _now(), run_row_id))
        conn.commit()
        print(f"ERROR: {exc}")
        conn.close()
        return 1

    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, result_count = ?, "
        "raw_path = ?, apify_run_id = ? WHERE id = ?",
        (_now(), len(result.items), str(result.raw_path), result.run_id, run_row_id),
    )
    conn.commit()

    report = ingest.ingest_items(conn, result.items, accounts.own, run_row_id)
    print(f"  enriched: {report.summary()}")

    # Comments live in a SEPARATE dataset the actor writes them to; they are
    # not embedded in the post items.
    comment_rows = apify.fetch_comments(result.items)
    written = ingest.ingest_comments(conn, comment_rows)
    print(f"  comments: {written} kept of {len(comment_rows)} scraped "
          f"(shorter than {ingest.MIN_COMMENT_CHARS} chars filtered out)")
    conn.close()
    return 0


def cmd_export(args) -> int:
    from . import briefs

    conn = _open_db()
    written = export.to_csv(conn, config.EXPORT_DIR)
    print(f"Wrote {len(written)} CSVs to "
          f"{config.EXPORT_DIR.relative_to(config.ROOT)}/")
    for p in written:
        print(f"  {p.name}")

    triage = export.triage_csv(conn, config.EXPORT_DIR)
    if triage:
        print(f"  {triage.name}  (idea triage)")

    brief_paths = briefs.export_briefs(conn, config.EXPORT_DIR / "briefs")
    if brief_paths:
        print(f"Wrote {len(brief_paths)} content briefs to "
              f"{(config.EXPORT_DIR / 'briefs').relative_to(config.ROOT)}/")
        for p in brief_paths:
            print(f"  {p.name}")

    url = export.to_sheets(conn, args.sheet_name)
    if url:
        print(f"  Google Sheet: {url}")
    else:
        print("  Google Sheets skipped (set GOOGLE_SERVICE_ACCOUNT_JSON and "
              "`pip install gspread` to enable)")
    conn.close()
    return 0


def cmd_status(args) -> int:
    if not config.DB_PATH.exists():
        print("No store yet. Run `python -m engine init`.")
        return 1
    conn = _open_db()
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731

    print("Store:", config.DB_PATH)
    print(f"  accounts          {q('SELECT COUNT(*) FROM accounts')}")
    print(f"  posts             {q('SELECT COUNT(*) FROM posts')}")
    print(f"    carousels       {q('SELECT COUNT(*) FROM posts WHERE is_slideshow=1')}")
    print(f"  slide images      {q('SELECT COUNT(*) FROM post_images')}")
    print(f"  comments          {q('SELECT COUNT(*) FROM comments')}")
    print(f"  scored            {q('SELECT COUNT(*) FROM post_scores')}")
    print(f"    selected        {q('SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL')}")
    print(f"  ideas             {q('SELECT COUNT(*) FROM ideas')}")
    print(f"  scrape runs       {q('SELECT COUNT(*) FROM scrape_runs')}")

    failed = q("SELECT COUNT(*) FROM scrape_runs WHERE error IS NOT NULL")
    if failed:
        print(f"  FAILED runs       {failed}")
    conn.close()
    return 0


def cmd_run(args) -> int:
    for step in (cmd_scrape, cmd_score, cmd_enrich, cmd_export):
        code = step(args)
        if code != 0:
            return code
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # `--yes` lives on a shared parent so it is accepted both before and after
    # the subcommand. `engine scrape --yes` is the form people actually type.
    # SUPPRESS matters: without it the subparser's default would overwrite a
    # `--yes` given before the subcommand, silently re-prompting.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--yes", action="store_true",
                        default=argparse.SUPPRESS,
                        help="skip confirmation before spending money")

    parser = argparse.ArgumentParser(
        prog="python -m engine",
        description="Competitor-driven TikTok content intelligence pipeline.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common],
                   help="create config files and an empty store")
    sub.add_parser("scrape", parents=[common],
                   help="Pass A: wide sweep over tracked accounts")
    sub.add_parser("score", parents=[common],
                   help="baselines, scores, and analysis-batch selection")
    sub.add_parser("enrich", parents=[common],
                   help="Pass B: comments and slide images for the batch")
    sub.add_parser("status", parents=[common],
                   help="what is in the store right now")
    sub.add_parser("run", parents=[common],
                   help="scrape -> score -> enrich -> export")

    exp = sub.add_parser("export", parents=[common],
                         help="write CSVs and optionally Google Sheets")
    exp.add_argument("--sheet-name", default="TikTok Content Intelligence")

    return parser


COMMANDS = {
    "init": cmd_init,
    "scrape": cmd_scrape,
    "score": cmd_score,
    "enrich": cmd_enrich,
    "export": cmd_export,
    "status": cmd_status,
    "run": cmd_run,
}


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Both are SUPPRESS-or-absent depending on where the flag landed.
    args.yes = getattr(args, "yes", False)
    if not hasattr(args, "sheet_name"):
        args.sheet_name = "TikTok Content Intelligence"
    try:
        return COMMANDS[args.command](args)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
