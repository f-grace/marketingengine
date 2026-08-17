---
status: ACTIVE
---
# Competitor-Driven TikTok Content Intelligence Pipeline

Revised 2026-08-17 after scope correction.
Mode: SELECTIVE EXPANSION | Language: Python | Store: SQLite + Sheets export

---

## 0. What this is, and what it is not

**This is the intelligence half.** It scrapes competitors, works out what is
actually working and why, and produces approved content concepts.

**It is not the generation half.** You already have a TikTok slideshow
generation system. This pipeline hands concepts to it and later reads results
back from it. Everything about image models, rendering, and publishing lives in
that existing system and is explicitly out of scope here.

The boundary between the two is one thing: **a UUID that survives the handoff.**
That is the most important integration detail in this document.

---

## 1. Roadmap

```
  ┌── PHASE 1: SEE ───────────────────────────────────────────────┐
  │  1. SCRAPE      Apify clockworks/tiktok-scraper, Pass A       │
  │  2. BASELINE    per-creator medians                           │
  │  3. SCORE       save-rate, engagement, reach vs own baseline  │
  │  4. SELECT      30 winners + 10 losers + 10 anomalies         │
  │  5. ENRICH      Pass B: comments + slideshow images           │
  │       ↓                                                        │
  │     SQLite  ──auto-export──>  Google Sheets (inspect/debug)   │
  └───────────────────────────────────────────────────────────────┘
                              ↓
  ┌── PHASE 2: UNDERSTAND ────────────────────────────────────────┐
  │  6. ANALYZE     AI reads captions + every slide image,        │
  │                 emits structured attributes per post          │
  │  7. CLUSTER     group on attributes (NOT with an LLM)         │
  │  8. CONTRAST    winners vs losers -> what actually separates  │
  │  9. GENERATE    our version of what won: same hashtags,       │
  │                 same structure, our topic and hook            │
  │       ↓                                                        │
  │ 10. GATE 1      you approve / edit / reject the CONCEPT       │
  └───────────────────────────────────────────────────────────────┘
                              ↓
                    [ handoff: idea_uuid ]
                              ↓
  ┌── YOUR EXISTING GENERATION SYSTEM ────────────────────────────┐
  │  generate slides  ->  GATE 2 (approve output)  ->  publish    │
  └───────────────────────────────────────────────────────────────┘
                              ↓
  ┌── PHASE 3: LEARN ─────────────────────────────────────────────┐
  │ 11. MEASURE     scrape your own posts, join on idea_uuid      │
  │ 12. REWEIGHT    your results progressively outrank competitor │
  │                 signal in what gets prioritized               │
  └───────────────────────────────────────────────────────────────┘
```

---

## 2. Step detail

### Step 1 — Scrape (Pass A, wide and cheap)

`clockworks/tiktok-scraper`, pay-per-event at ~**$1.70 / 1,000 results**.

**Include your own handle in `profiles` from day 1.** Analysis of your own posts
is Phase 3, but the *collection* starts now. Baseline math needs history and
history is the one thing you cannot backfill. Scraping your own account costs
pennies and means Phase 3 opens with months of data instead of zero.

```json
{
  "profiles": ["YOUR_OWN_HANDLE", "competitor1", "competitor2", "..."],
  "profileScrapeSections": ["videos"],
  "profileSorting": "latest",
  "resultsPerPage": 100,
  "excludePinnedPosts": false,
  "maxFollowersPerProfile": 0,
  "maxFollowingPerProfile": 0,
  "commentsPerPost": 0,
  "topLevelCommentsPerPost": 0,
  "maxRepliesPerComment": 0,
  "scrapeRelatedVideos": false,
  "scrapeAdditionalAuthorMeta": false,
  "shouldDownloadVideos": false,
  "shouldDownloadCovers": false,
  "shouldDownloadSlideshowImages": false,
  "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
  "aiVideoDescription": false,
  "aiVideoSummary": false,
  "proxyCountryCode": "US"
}
```

Persist the raw dataset JSON to disk **before parsing**. Scoring becomes
re-runnable without re-scraping, and an actor schema change shows up as a parse
error you can diff instead of a mystery.

Keep `aiVideoDescription` and `aiVideoSummary` false. Unverified, but per-item
AI enrichment on a pay-per-event actor is very likely billed extra and you run
your own analysis downstream. Confirm before ever enabling.

**Verify before first run:** the allowed values for `profileSorting` are not
enumerated in the README (default is `"latest"`), and `oldestPostDateUnified` is
typed `string` with no documented format. Check both in Apify Console.

### Step 2 — Baseline

Per creator, over their last 30 posts:

```
  median_plays, median_saves, median_engagement
```

This is what makes the whole thing work. A post with 40k views on an account
that averages 8k is a hit. A post with 200k views on an account that averages
400k is a flop. Absolute numbers get both backwards.

### Step 3 — Score

```
  save_rate    = collectCount / max(playCount, 1)
  engage_rate  = (diggCount + commentCount + shareCount + collectCount) / max(playCount, 1)
  reach_index  = playCount / median_plays(author)
  recency      = exp(-days_since_post / 21)

  composite    = (0.45*z(save_rate) + 0.25*z(engage_rate) + 0.30*z(reach_index)) * recency
```

Saves carry the most weight because saves are the strongest quality signal the
carousel algorithm reads. Z-scores computed **within the carousel cohort only**
(`isSlideshow == true`); mixing video and photo posts into one distribution
makes every statistic meaningless.

**Guard (from review Finding 4):** if cohort `stdev < epsilon`, return a neutral
score rather than dividing. If the cohort is below a minimum size, refuse to
rank at all and surface "not enough carousel data, widen the account list."
A thin cohort produces confident-looking nonsense, which is worse than an error.

### Step 4 — Select (three selectors, not one)

| Selector | N | What it answers |
|---|---|---|
| **Winners** | ~30 | What is working right now |
| **Losers** | ~10 | The control group. Without it you cannot tell a driver from a universal trait |
| **Anomalies** | ~10 | What is *newly* working, before it saturates |

**Why losers matter.** Analyze only winners and "has a strong hook" looks
predictive, because every winner has a hook and you never checked that every
loser did too. The control set is what converts pattern-matching into something
you can act on.

**Anomaly definition (computable pre-LLM):** posts whose *engagement shape*
departs from the account's norm, not just its magnitude. Specifically, unusually
high `save_rate` relative to that account's typical `save_rate : digg_rate`
ratio. Those are posts people wanted to keep rather than merely enjoyed, and
they surface emerging formats earlier than raw reach does.

### Step 5 — Enrich (Pass B, narrow and rich)

Only the ~50 selected posts:

```json
{
  "postURLs": ["https://www.tiktok.com/@x/video/123", "..."],
  "topLevelCommentsPerPost": 30,
  "maxRepliesPerComment": 0,
  "shouldDownloadSlideshowImages": true,
  "shouldDownloadCovers": true,
  "aiVideoDescription": false,
  "aiVideoSummary": false,
  "proxyCountryCode": "US"
}
```

Comments and image downloads are per-post multipliers on billed results.
Applying them to all 1,000 posts instead of the 50 you care about is where a
$1.70 run becomes a $40 run.

`maxRepliesPerComment: 0` because top-level comments are the questions your
market is asking, while replies are commenters talking to each other. You pay
per comment, so buy only the signal.

### Step 6 — Analyze

One AI call per selected post, reading the caption **and every slide image**.
Structured output:

```json
{
  "topic": "...",
  "hook_text": "...",
  "hook_shape": "contrarian_correction | numbered_list | before_after | ...",
  "target_audience": "...",
  "pain_point": "...",
  "content_angle": "...",
  "slide_structure": [{"slide": 1, "role": "hook"}, {"slide": 4, "role": "payoff"}],
  "visual_style": "...",
  "cta_type": "...",
  "cta_slide": 6,
  "why_it_performed": "...",
  "relevance_to_us": 0-10,
  "repeatability": 0-10
}
```

**`repeatability` is the field that earns its keep.** It separates "they caught
a viral moment" from "this is a mechanism we can run weekly." Weight it heavily
in step 9 selection, not just record it.

Losers go through the identical prompt. Same schema, same fields. The contrast
in step 8 only works if both sets are described in the same vocabulary.

**Security note (review Finding 3):** competitor captions, comments, and text
*inside slide images* are untrusted third-party input going into an LLM and a
vision model. Wrap all of it in explicit data delimiters and state in the system
prompt that its contents are data, never instructions. The realistic worst case
is one manipulated concept that Gate 1 rejects, but the fix is nearly free.

### Step 7 — Cluster

Group on the structured attributes from step 6 using ordinary grouping or
embeddings. **Do not ask an LLM to compare posts.** It costs more, it is
non-deterministic, and it will silently change its mind between runs. The LLM's
job was turning unstructured posts into structured attributes; that job is done.

### Step 8 — Contrast

For each attribute, compare its frequency in winners against losers. An
attribute that appears in 90% of winners and 85% of losers explains nothing.
An attribute at 60% versus 15% is a real signal. This step is plain arithmetic
and it is what makes the analysis honest.

### Step 9 — Generate (replication-flavored)

Runs **after** analysis, on the posts that actually won. The model knows which
posts performed and why, and produces our version of them.

**Concrete carryover from the winner.** Not vibes, specific fields:

| Carried over | Adapted to us | Never copied |
|---|---|---|
| `hashtags` (same set) | topic and angle | exact caption text |
| caption *structure* and length | pain point, from our audience | claims about their product |
| `slide_count` and slide roles | hook wording | their imagery |
| `hook_shape` | CTA target | their brand references |
| `musicMeta.musicId` if trending | | |

Hashtags carry over verbatim. That is deliberate: hashtags are distribution
plumbing, not creative, and matching a winner's tag set is one of the cheapest
ways to land in the same feed. Same logic for a trending `musicId`.

**One-to-one vs cluster-to-one.** When a single post is a standout, generate
from that post. When several winners share a mechanism (step 7 put them in one
cluster), generate from the cluster instead. Same step, and the cluster case
produces the stronger concept because the mechanism is validated more than once.

Inputs to the generation call:
1. The winner (or cluster) with its full structured analysis
2. The loser contrast from step 8, so the model knows what to avoid
3. Audience questions from the comments in step 5
4. Your product truth from `brand.json`

Output per concept: topic, hook, caption draft, hashtag set, slide-by-slide
outline, suggested sound, and a `source_analysis_ids` list so Gate 1 can show
you exactly what it was modeled on.

### Step 10 — Gate 1 (concept approval)

You review: the source inspiration, the performance data, the AI's reasoning,
the proposed concept, hook, and direction. You edit, reject, or steer.

This gate sits **before** generation so rejecting is free. Gate 2, approving the
rendered output, lives in your existing system.

Every approved idea is assigned a **UUID at creation**.

### Steps 11-12 — Measure and learn (Phase 3)

Re-scrape your own posts with the same actor. Join back on `idea_uuid`. As your
own dataset grows, its weight in prioritization rises and competitor signal
falls. Competitor data never disappears; it becomes one input among several.

---

## 3. The handoff contract

This is the part that is unbuildable later if skipped now.

```
   ideas.id (UUID)  ──────>  your generation system  ──────>  published post
        │                                                          │
        └──────────────────  publications.tiktok_url  <────────────┘
```

Requirements:
1. Every idea gets a UUID at creation, before handoff.
2. The UUID travels into your existing system and is stored there.
3. When a post publishes, the UUID and the TikTok URL come back to this pipeline.

Without step 3, Phase 3 cannot exist, and you would not discover that for
months. **Open item: I have not seen your generation system, so I cannot specify
how it carries the UUID.** That needs scoping before Phase 2 handoff is built.

---

## 4. Data model (SQLite)

```sql
accounts(id, handle, added_at, active, notes)

scrape_runs(id, started_at, pass, actor_input_json, raw_path, result_count, cost_est)

posts(id, tiktok_id UNIQUE, account_id, scrape_run_id, url, caption,
      created_at, play_count, digg_count, comment_count, share_count,
      collect_count, is_slideshow, slide_count, music_id, hashtags_json, cover_url)

post_images(id, post_id, slide_index, url, local_path)

comments(id, post_id, text, digg_count)

account_baselines(account_id, computed_at, median_plays, median_saves,
                  median_engage, save_digg_ratio, n_posts)

post_scores(post_id, save_rate, engage_rate, reach_index, anomaly_score,
            composite, cohort, selected_as)      -- winner | loser | anomaly

analyses(id, post_id, model, created_at, topic, hook_text, hook_shape,
         target_audience, pain_point, content_angle, slide_structure_json,
         visual_style, cta_type, cta_slide, why_it_performed,
         relevance_to_us, repeatability, raw_json)

clusters(id, computed_at, label, member_count)
cluster_members(cluster_id, analysis_id)

attribute_contrasts(id, computed_at, attribute, value,
                    winner_freq, loser_freq, lift)

ideas(id TEXT PRIMARY KEY,          -- UUID. the join key.
      cluster_id, created_at, concept, hook, direction, rationale,
      source_analysis_ids_json, status, approved_at, edited_by_human)
      -- status: proposed | approved | edited | rejected | handed_off

handoffs(idea_id, handed_off_at, external_ref)
publications(id, idea_id, tiktok_url, published_at)
own_performance(publication_id, measured_at, play_count, collect_count,
                digg_count, comment_count, share_count)
```

Auto-export to Google Sheets after every run: one tab per table for
`posts`, `post_scores`, `analyses`, `attribute_contrasts`, `ideas`. Read-only
view for inspection and debugging. Edits happen in Gate 1, not in the sheet.

Idea state machine:

```
  proposed ──> approved ──> handed_off ──> published ──> measured
      │            ▲
      ├──> edited ─┘
      └──> rejected (terminal)
```

---

## 5. Cost model

Per weekly run:

| Line | Cost |
|---|---|
| Pass A: 10 accounts x 100 posts | ~$1.70 |
| Pass B: 50 posts x 30 comments + images | ~$3-5 |
| AI analysis: 50 posts x ~6 slide images (vision) | ~$3-5 |
| Clustering, contrast | $0 (local math) |
| Idea generation | ~$0.50 |
| **Weekly total** | **~$8-12** |
| **Monthly** | **~$40-50** |

Image generation and rendering costs are not here because they live in your
existing system.

The pre-filter is what keeps this cheap. Sending all 1,000 scraped posts through
vision analysis instead of 50 would cost roughly $60-100 per run, about
**20x more**, for signal you would then have to filter anyway.

---

## 6. Decisions log

| # | Decision | Chosen | Why |
|---|---|---|---|
| D1 | Architecture | Approach B, staged | Feedback loop in scope from day 1 |
| D2 | Review mode | Selective expansion | Baseline held, expansions cherry-picked |
| D4 | Video | Cut, carousels only | Video models orders of magnitude above image cost |
| E1 | Comment mining | Accepted | Same actor, no second integration |
| E3 | Brand lock + slop guard | Accepted | Quality by construction |
| E4 | Slide-1 scorer | Accepted | Now belongs to the generation system |
| E5 | Multi-platform | Deferred | Do not multiply unvalidated output |
| E6 | Provenance trail | Accepted | Review queue becomes a learning surface |
| — | Language | Python | Scoring and cohort statistics are the tricky half |
| — | Store | SQLite + Sheets export | Relational chain stays joinable, debuggability kept |
| — | Selection | Winners + losers + anomalies | Control group makes patterns actionable |
| — | Join key | UUID round-trip | Phase 3 is unbuildable without it |
| — | Clustering | Attributes, not LLM | Deterministic, reproducible, free |
| — | Human gates | Two | Concept approval before spend, output approval after |

---

## 7. Build phases

| Phase | Ships | Done when |
|---|---|---|
| **1. See** | Steps 1-5 + SQLite + Sheets export | You can look at a ranked, normalized table of competitor carousels |
| **2. Understand** | Steps 6-10 + Gate 1 | First approved concept handed to your generation system with a UUID |
| **3. Learn** | Steps 11-12 | First pattern identified from your own results rather than competitors' |

Phase 1 is worth shipping alone. Even with no generation attached, a ranked
baseline-normalized view of what is working in your niche is something you do
not currently have.

---

## 7b. Reference workflow and what we changed

An n8n workflow from "AI Watches TikTok So You Don't Have To" covers similar
ground:

```
form → Apify actor → append to Sheet → Aggregate → read Sheet
     → Loop Over Items → Analyze video (Gemini) → Message a model → update Sheet
```

Worth stealing: feeding media directly to Gemini for analysis rather than
extracting frames first. For slideshows that means one call per post carrying
all the slide images from `shouldDownloadSlideshowImages`.

Deliberately different:

| That workflow | This plan | Why |
|---|---|---|
| Analyzes every scraped item | Quantitative pre-filter to ~50 first | Multimodal calls on 950 posts you'll discard is where the cost is |
| No baseline normalization | Per-creator median, `reach_index` | Otherwise you systematically over-value big accounts |
| Winners only | Winners + losers + anomalies | No control group means universal traits look predictive |
| Sheet is the store | SQLite is the store, Sheet is a view | The feedback loop needs joins |
| LLM does everything | LLM extracts attributes, math does clustering | Deterministic and free |

## 8. Open items

1. **What the startup sells, to whom.** Still unspecified. Blocks
   `relevance_to_us` scoring, `brand.json`, and the ideation prompt. The
   pipeline is product-agnostic by design, but nothing useful comes out until
   this exists as config.
2. **The competitor account list.** Blocks step 1.
3. **Your existing generation system's interface.** Blocks the handoff contract
   in section 3 and therefore Phase 3.
4. **Two Apify schema values** to verify in Console: `profileSorting` allowed
   values, `oldestPostDateUnified` string format.
5. **Whether `aiVideoDescription` / `aiVideoSummary` are billed extras.**

## 9. Unresolved review findings

Raised in review, dismissed without a decision, recorded here rather than
dropped:

| # | Finding | Applies to |
|---|---|---|
| F1 | Silent font fallback in headless Chrome renders off-brand images that pass every check | **Your existing generation system**, not this pipeline. Still worth checking there |
| F2 | No rate limit or kill switch between generator and platform; publishing is the only irreversible action | **Your existing generation system.** A daily cap and a global hold flag are cheap insurance |
| F3 | Prompt injection via scraped captions, comments, and text inside slide images | **This pipeline.** Mitigation written into step 6 above as the recommended default; not yet decided |
| F4 | Zero-variance z-scores produce NaN in thin cohorts | **This pipeline.** Guard written into step 3 above as the recommended default; not yet decided |

F3 and F4 are written into the plan as recommendations. Say the word if you want
either changed or removed.
