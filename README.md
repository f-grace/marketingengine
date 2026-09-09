# TikTok Recreation-Prompt Engine

Scrapes your own TikTok account and the idea-source accounts in your niche,
finds the single-image photo posts that beat their creator's baseline, and
emails you a paste-ready recreation prompt (plus the source image) for each
one. You feed the prompt and image to your own multimodal LLM / image tool;
this engine deliberately contains no LLM of its own.

Two pathways, decided by who posted the winner:

- **`own_rebrand`** — one of *your* posts beat your baseline: the prompt asks
  for a fresh variant (keep the layout and hook, reword everything, swap
  logos/branding).
- **`source_recreate`** — a source account's post went viral: the prompt asks
  for the concept rebuilt in your voice with every trace of the original
  account stripped.

Historical design notes and the cost model: [PLAN.md](PLAN.md) (describes the
earlier carousel-analysis incarnation; the scoring model is unchanged).

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m engine init
```

`init` creates `config/accounts.yml`, `config/brand.json`, and `.env` from the
tracked templates, plus an empty SQLite store.

Then:

1. Put your Apify token in `.env` (get one at
   [console.apify.com/settings/integrations](https://console.apify.com/settings/integrations)).
   It is read from the environment only and never written to the store.
2. For email delivery, set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and
   `DIGEST_TO` in `.env`. The password is a Google App Password (Google
   Account → Security → 2-Step Verification → App passwords; 2-Step
   Verification must be on). Without these, prompts stay on disk under
   `data/exports/prompts/`.
3. List your own handle under `own:` and idea-source accounts under `sources:`
   in `config/accounts.yml`. The actor bills ~$3.70/1k results against a
   $5/month free tier, so start narrow.
4. Keep `config/brand.json` honest — its voice, guardrails, and phase rules
   are pasted into every recreation prompt.

## Use

```bash
./.venv/bin/python -m engine run --yes   # the whole thing:
                                         # scrape -> score -> enrich -> prompts -> email
```

Or step by step:

```bash
./.venv/bin/python -m engine scrape             # Pass A wide sweep. The only step that costs money.
./.venv/bin/python -m engine score              # baselines + scores over single-image posts, pick winners
./.venv/bin/python -m engine enrich             # download each winner's image (free, direct from CDN)
./.venv/bin/python -m engine prompts            # write one recreation prompt .md per new winner
./.venv/bin/python -m engine prompts --dry-run  # preview to stdout, write nothing
./.venv/bin/python -m engine email              # send unemailed prompts + images to DIGEST_TO
./.venv/bin/python -m engine email --dry-run    # show what would be sent
./.venv/bin/python -m engine export             # CSV views of the store
./.venv/bin/python -m engine status             # what is in the store
```

`scrape` prints a cost estimate and asks before calling the actor; pass
`--yes` to skip the prompt (before or after the subcommand). Everything else
is free and local.

Timing matters for `enrich`: TikTok's CDN image URLs are signed and expire
within hours, which is why `run` downloads immediately after scraping and why
the *file* (not the URL) is what gets attached to the email. If direct CDN
downloads ever start failing consistently, the actor can rehost images on
Apify storage (see the contingency note in `engine/apify.py`).

## How it works

```
  scrape ──> baseline ──> score ──> select ──> enrich ──> prompts ──> email
             per-creator  save-rate  winners +  image     one .md     digest +
             medians over weighted   reach-only download  per winner  attachments
             single-image
             posts only
```

Three ideas do the heavy lifting:

**Baseline-relative ranking.** A post is judged against what *that creator*
normally gets, not against absolute numbers. 40k views on an account averaging
8k is a hit; 200k on an account averaging 400k is a flop. Ranking on raw views
gets both backwards, and that is the most common failure in competitor scraping.

**One format, one denominator.** Only single-image photo posts are scored, and
each account's baseline is computed over its single-image posts only. Mixing
videos or multi-slide carousels into the denominator makes every statistic
meaningless (observed live in this pipeline's carousel era).

**Idempotent output.** Each winning post gets at most one prompt ever
(`recreation_prompts.post_id` is UNIQUE), and `emailed_at` is stamped only
after a successful send — so reruns never duplicate work or emails, and a
failed send just leaves prompts queued for the next `engine email`.

## Layout

```
engine/
  scoring.py     pure math: rates, reach index, z-scores, recency. No I/O.
  selection.py   pure: winners / reach-only, guaranteed disjoint
  pipeline.py    baselines, scoring, selection against the store
  apify.py       actor client, Pass A input builder, cost guardrails
  ingest.py      actor output -> rows, tolerant of missing and malformed fields
  prompts.py     recreation-prompt templates (pure) + generation/dedupe
  notify.py      Gmail digest: build + send, stdlib SMTP only
  db.py          SQLite schema
  export.py      CSV views
  config.py      accounts, scrape settings, brand, secrets from env only
  cli.py         command line
config/          .example files are tracked; your filled-in copies are not
data/            store, raw datasets, images, prompts. Gitignored.
```

`scoring.py`, `selection.py`, and `prompts.build_prompt` are pure functions
with no database or network access, which is why the analytical core is cheap
to test.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

149 tests, no network calls, no API keys needed. The integration tests run real
ingest, scoring, prompt generation, and export against synthetic actor output;
SMTP is monkeypatched, never dialled.

Worth knowing about two of them:

- `test_small_account_outranks_large_account` guards the baseline normalization.
  The bug it catches produces plausible-looking rankings rather than an error.
- `test_identical_posts_produce_no_nan` guards zero-variance z-scores, which
  otherwise emit NaN that propagates silently downstream.

## Caveats

- The single-image cohort is a subset of what accounts post. If scoring warns
  that the cohort is below the minimum for reliable ranking, raise
  `results_per_page` or add more `sources:` accounts.
- Scraped captions are third-party text; each prompt labels them as reference
  material so instruction-shaped captions are less likely to steer your LLM.
- Before a first real run, verify in Apify Console the two undocumented actor
  fields: allowed values for `profileSorting`, and the accepted format of
  `oldestPostDateUnified`.
