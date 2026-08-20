# TikTok Content Intelligence Pipeline

Scrapes competitor TikTok accounts, works out what is actually working and why,
and selects the posts worth analysing. Feeds concepts into your existing
slideshow generation system.

Full design, cost model, and open questions: [PLAN.md](PLAN.md).

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
2. List your own handle and competitors in `config/accounts.yml`. Start with
   2-3 to validate cheaply; the actor bills ~$3.70/1k results against a $5/month
   free tier, so a full 10-account sweep is most of a month's credit.
3. Fill in `config/brand.json` when you get to generation. Phase 1 runs without it.

## Use

```bash
./.venv/bin/python -m engine scrape    # Pass A: wide sweep, ~$3.70 per 1k results
./.venv/bin/python -m engine score     # baselines, scores, pick the batch
./.venv/bin/python -m engine enrich    # Pass B: comments + slide images
./.venv/bin/python -m engine export    # CSVs, briefs, idea triage
./.venv/bin/python -m engine render    # slide PNGs, $0 each
./.venv/bin/python -m engine status    # what is in the store
```

The engine stops at rendered slides and written briefs. Posting is deliberately
out of scope: you review the deck and upload it yourself, which is the right
shape while the account is new and one bad post costs distribution.

Or the lot: `python -m engine run`

`scrape` and `enrich` cost money. Both print an estimate and ask before calling
the actor. Pass `--yes` to skip the prompt (works before or after the
subcommand).

## How it works

```
  scrape ──> baseline ──> score ──> select ──> enrich ──> export
             per-creator  save-rate  winners   comments   CSV +
             medians      weighted   losers    + slide    Sheets
                                     anomalies  images
```

Three ideas do the heavy lifting:

**Baseline-relative ranking.** A post is judged against what *that creator*
normally gets, not against absolute numbers. 40k views on an account averaging
8k is a hit; 200k on an account averaging 400k is a flop. Ranking on raw views
gets both backwards, and that is the most common failure in competitor scraping.

**A control group.** The analysis batch is 30 winners, 10 losers, and 10
anomalies. Without the losers, every attribute winners share looks predictive,
including the ones losers share too.

**Two-pass scraping.** Comments and image downloads are per-post multipliers on
billed results. Pass A sweeps wide and cheap; Pass B enriches only the ~50 posts
that made the batch. Applying enrichment to all 1,000 is the difference between
a $0.56 validation run and a $9+ one.

## Layout

```
engine/
  scoring.py     pure math: rates, reach index, z-scores, recency. No I/O.
  selection.py   pure: winners / losers / anomalies, guaranteed disjoint
  pipeline.py    baselines, scoring, selection against the store
  apify.py       actor client, Pass A and Pass B input builders
  ingest.py      actor output -> rows, tolerant of missing and malformed fields
  db.py          SQLite schema, idea state machine
  export.py      CSV always, Google Sheets when credentials exist
  config.py      accounts, scrape settings, brand, secrets from env only
  render.py      slide PNGs: stock photo + outlined type, drawn not generated
  imagery.py     Pexels backgrounds, cached; gradient fallback with no key
  briefs.py      slide-by-slide content briefs
  cli.py         command line
config/          .example files are tracked; your filled-in copies are not
data/            store, raw datasets, exports. Gitignored.
```

`scoring.py` and `selection.py` are pure functions with no database or network
access, which is why the analytical core is cheap to test.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

141 tests, no network calls, no API keys needed. The integration tests run real
ingest, scoring, and export against synthetic actor output.

Worth knowing about two of them:

- `test_small_account_outranks_large_account` guards the baseline normalization.
  The bug it catches produces plausible-looking rankings rather than an error.
- `test_identical_posts_produce_no_nan` guards zero-variance z-scores, which
  otherwise emit NaN that propagates silently downstream.

## Status

Phase 1 (scrape, score, select, export) is built and tested. Phase 2 (AI
analysis, clustering, winner-vs-loser contrast, generation, human gate) has its
schema in place but no code yet. Phase 3 (own-post feedback loop) depends on the
UUID handoff contract described in PLAN.md section 3.

Before a first real run, verify two things in Apify Console that the actor README
leaves undocumented: the allowed values for `profileSorting`, and the accepted
format of `oldestPostDateUnified`.
