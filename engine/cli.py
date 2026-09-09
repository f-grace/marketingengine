"""Command line entry point.

    python -m engine init      create config files and an empty store
    python -m engine scrape    Pass A: wide sweep over every tracked account
    python -m engine score     baselines, scores, and pick the recreation batch
    python -m engine enrich    download the single image for each selected post
    python -m engine prompts   write recreation-prompt .md files for new winners
    python -m engine email     send the digest of not-yet-emailed prompts
    python -m engine export    write CSV views of the store
    python -m engine status    what is in the store right now
    python -m engine run       scrape -> score -> enrich -> prompts -> email

Only `scrape` costs money. It prints an estimate and, unless you pass --yes,
asks before calling the actor. Everything else is local (enrich downloads
straight from the CDN for free).
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import apify, config, db, export, ingest, notify, pipeline, prompts


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
    print("  1. Put your Apify token (and Gmail app password) in .env")
    print("  2. List your handle and idea-source accounts in config/accounts.yml")
    print("  3. python -m engine run")
    return 0


def cmd_scrape(args) -> int:
    accounts = config.load_accounts()
    settings = config.load_scrape_settings()
    token = config.apify_token()

    # Accounts already in the store only need a shallow refresh: it picks up
    # posts added since the last run AND updates play counts on recent posts,
    # which is how a post that goes viral days after publishing still gets
    # caught. Unseen accounts get one deep scrape to build their baseline.
    conn = _open_db()
    known = pipeline.known_account_handles(conn)
    deep = [h for h in accounts.all_handles if h.lower() not in known]
    refresh = [h for h in accounts.all_handles if h.lower() in known]

    batches = []
    if deep:
        batches.append(("deep (first scrape)", deep, settings.results_per_page))
    if refresh:
        batches.append(("refresh", refresh, settings.results_per_page_refresh))

    est = sum(len(h) * rpp for _, h, rpp in batches) / 1000.0 \
        * apify.USD_PER_1000_RESULTS

    print(f"Pass A over {len(accounts.all_handles)} accounts")
    for label, handles, rpp in batches:
        print(f"  {label}: {len(handles)} accounts x {rpp} posts "
              f"({', '.join(handles)})")
    print(f"  own: {', '.join(accounts.own) or '(none set)'}")
    print(f"  estimated cost: ~${est:.2f} (directional, not a quote)")

    warning = _budget_warning(est)
    if warning:
        print(f"  BUDGET: {warning}")

    if not accounts.own:
        print("  NOTE: no 'own' handle configured. Add it so your own winners "
              "get rebrand prompts and baseline history accumulates.")

    if not _confirm("Run the actor?", args.yes):
        print("Aborted.")
        conn.close()
        return 1

    for label, handles, rpp in batches:
        run_input = apify.pass_a_input(
            handles,
            results_per_page=rpp,
            oldest_post_date=settings.oldest_post_date,
            proxy_country_code=settings.proxy_country_code,
        )

        cur = conn.execute(
            "INSERT INTO scrape_runs (started_at, pass_name, actor_input_json) "
            "VALUES (?, 'A', ?)",
            (_now(), str(run_input)),
        )
        run_row_id = cur.lastrowid
        conn.commit()

        try:
            result = apify.run_actor(token, run_input, raw_dir=config.RAW_DIR,
                                     pass_name="A")
        except apify.ApifyError as exc:
            conn.execute(
                "UPDATE scrape_runs SET error = ?, finished_at = ? WHERE id = ?",
                (str(exc), _now(), run_row_id))
            conn.commit()
            conn.close()
            print(f"ERROR: {exc}")
            return 1

        conn.execute(
            "UPDATE scrape_runs SET finished_at = ?, result_count = ?, "
            "raw_path = ?, apify_run_id = ? WHERE id = ?",
            (_now(), len(result.items), str(result.raw_path), result.run_id,
             run_row_id),
        )
        conn.commit()

        print(f"  [{label}] {len(result.items)} results, "
              f"~${result.estimated_cost_usd:.2f}")
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
        print("\n  No single-image posts found. Either the tracked accounts do "
              "not post one-image photo posts, or nothing has been scraped yet.")
    conn.close()
    return 0


# TikTok CDN serves signed URLs that expire within hours, and rejects requests
# without a browser-looking user agent. Download immediately after scrape.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _download(url: str, dest: Path, timeout: int = 15) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False
    if not data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def cmd_enrich(args) -> int:
    """Download the single image for each selected post. Free, local."""
    conn = _open_db()
    selected = pipeline.selected_posts(conn)
    if not selected:
        print("Nothing selected. Run `python -m engine score` first.")
        conn.close()
        return 1

    downloaded = skipped = failed = 0
    for row in selected:
        image = conn.execute(
            "SELECT id, url, local_path FROM post_images "
            "WHERE post_id = ? AND slide_index = 0",
            (row["id"],),
        ).fetchone()
        url = image["url"] if image else None
        if not url:
            cover = conn.execute(
                "SELECT cover_url FROM posts WHERE id = ?", (row["id"],)
            ).fetchone()
            url = cover["cover_url"] if cover else None
        if not url:
            failed += 1
            continue

        dest = config.IMAGES_DIR / f"{row['tiktok_id']}.jpg"
        if dest.exists():
            skipped += 1
        elif _download(url, dest):
            downloaded += 1
        else:
            failed += 1
            print(f"  could not fetch image for @{row['tiktok_id']} "
                  "(expired or blocked); its prompt will carry the URL only")
            continue

        if image:
            conn.execute("UPDATE post_images SET local_path = ? WHERE id = ?",
                         (str(dest), image["id"]))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO post_images "
                "(post_id, slide_index, url, local_path) VALUES (?, 0, ?, ?)",
                (row["id"], url, str(dest)),
            )
    conn.commit()

    print(f"Images: {downloaded} downloaded, {skipped} already on disk, "
          f"{failed} failed")
    conn.close()
    return 0


def cmd_prompts(args) -> int:
    brand = config.load_brand()
    conn = _open_db()

    if args.dry_run:
        sources = prompts._sources_for_selected(conn)
        if not sources:
            print("Nothing selected. Run `python -m engine score` first.")
            conn.close()
            return 1
        for src in sources:
            print("=" * 72)
            print(prompts.build_prompt(src, brand))
        print(f"(dry run: {len(sources)} prompts printed, nothing written)")
        conn.close()
        return 0

    written = prompts.generate(conn, brand, config.PROMPTS_DIR, force=args.force)
    if not written:
        print("0 new prompts (every selected post already has one; "
              "use --force to regenerate).")
    else:
        print(f"Wrote {len(written)} recreation prompts to "
              f"{config.PROMPTS_DIR.relative_to(config.ROOT)}/")
        for p in written:
            print(f"  {p.prompt_path.name}  ({p.pathway})")
    conn.close()
    return 0


def cmd_email(args) -> int:
    conn = _open_db()
    pending = prompts.unemailed(conn)
    if not pending:
        print("Nothing unemailed. Run `python -m engine prompts` first.")
        conn.close()
        return 0

    gmail = config.gmail_settings()
    if gmail is None:
        print("Gmail is not configured. Set GMAIL_ADDRESS and "
              "GMAIL_APP_PASSWORD (and optionally DIGEST_TO) in .env — see "
              ".env.example. The prompts are on disk under "
              f"{config.PROMPTS_DIR.relative_to(config.ROOT)}/")
        conn.close()
        return 1

    messages = notify.build_digests(pending, to=gmail.to, sender=gmail.address)

    if args.dry_run:
        for msg in messages:
            attachments = [part.get_filename() for part in msg.iter_attachments()]
            print(f"To: {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            print(f"Attachments ({len(attachments)}): "
                  + ", ".join(a for a in attachments if a))
            print()
            print(msg.get_body(("plain",)).get_content())
        print(f"(dry run: {len(messages)} message(s) built, nothing sent)")
        conn.close()
        return 0

    sent_ids = []
    for i, (msg, batch) in enumerate(
        zip(messages, notify._batches(pending)), start=1
    ):
        try:
            notify.send(msg, gmail.address, gmail.app_password)
        except Exception as exc:
            print(f"ERROR sending message {i}/{len(messages)}: {exc}")
            print("  Unsent prompts stay queued; rerun `python -m engine email`.")
            break
        sent_ids.extend(p.post_id for p in batch)
        print(f"  sent {msg['Subject']} to {gmail.to}")

    if sent_ids:
        prompts.mark_emailed(conn, sent_ids)
        print(f"Delivered {len(sent_ids)} prompts.")
    conn.close()
    return 0 if len(sent_ids) == len(pending) else 1


def cmd_export(args) -> int:
    conn = _open_db()
    written = export.to_csv(conn, config.EXPORT_DIR)
    print(f"Wrote {len(written)} CSVs to "
          f"{config.EXPORT_DIR.relative_to(config.ROOT)}/")
    for p in written:
        print(f"  {p.name}")
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
    print(f"    single-image    {q('SELECT COUNT(*) FROM posts WHERE is_slideshow=1 AND slide_count=1')}")
    print(f"  scored            {q('SELECT COUNT(*) FROM post_scores')}")
    print(f"    selected        {q('SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL')}")
    print(f"  prompts           {q('SELECT COUNT(*) FROM recreation_prompts')}")
    print(f"    unemailed       {q('SELECT COUNT(*) FROM recreation_prompts WHERE emailed_at IS NULL')}")
    print(f"  scrape runs       {q('SELECT COUNT(*) FROM scrape_runs')}")

    failed = q("SELECT COUNT(*) FROM scrape_runs WHERE error IS NOT NULL")
    if failed:
        print(f"  FAILED runs       {failed}")
    conn.close()
    return 0


def cmd_run(args) -> int:
    for step in (cmd_scrape, cmd_score, cmd_enrich, cmd_prompts):
        code = step(args)
        if code != 0:
            return code
    if args.no_email:
        print("Email skipped (--no-email). Prompts are on disk.")
        return 0
    # Email failure is a warning, not a run failure: the prompts are already
    # written and stay queued for the next `engine email`.
    code = cmd_email(args)
    if code != 0:
        print("WARNING: email step did not complete; prompts remain queued.")
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
        description="TikTok single-image recreation-prompt pipeline.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common],
                   help="create config files and an empty store")
    sub.add_parser("scrape", parents=[common],
                   help="Pass A: wide sweep over tracked accounts")
    sub.add_parser("score", parents=[common],
                   help="baselines, scores, and recreation-batch selection")
    sub.add_parser("enrich", parents=[common],
                   help="download the single image for each selected post")
    sub.add_parser("status", parents=[common],
                   help="what is in the store right now")
    sub.add_parser("export", parents=[common],
                   help="write CSV views of the store")

    prm = sub.add_parser("prompts", parents=[common],
                         help="write recreation prompts for new winners")
    prm.add_argument("--dry-run", action="store_true",
                     help="print prompts to stdout, write and record nothing")
    prm.add_argument("--force", action="store_true",
                     help="regenerate prompts for the current selection")

    eml = sub.add_parser("email", parents=[common],
                         help="send the digest of not-yet-emailed prompts")
    eml.add_argument("--dry-run", action="store_true",
                     help="print the digest instead of sending it")

    run = sub.add_parser("run", parents=[common],
                         help="scrape -> score -> enrich -> prompts -> email")
    run.add_argument("--no-email", action="store_true",
                     help="skip the email step; prompts stay on disk")

    return parser


COMMANDS = {
    "init": cmd_init,
    "scrape": cmd_scrape,
    "score": cmd_score,
    "enrich": cmd_enrich,
    "prompts": cmd_prompts,
    "email": cmd_email,
    "export": cmd_export,
    "status": cmd_status,
    "run": cmd_run,
}


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # These are SUPPRESS-or-absent depending on subcommand and flag position.
    args.yes = getattr(args, "yes", False)
    args.dry_run = getattr(args, "dry_run", False)
    args.force = getattr(args, "force", False)
    args.no_email = getattr(args, "no_email", False)
    try:
        return COMMANDS[args.command](args)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
