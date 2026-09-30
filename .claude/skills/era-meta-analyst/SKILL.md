---
name: era-meta-analyst
description: Era's own Meta Ads analyst — the free, in-house replacement for paid analysis plugins (HYPD, Adspirer). Pulls campaign data through Meta's official facebook-ads connector and applies a senior analyst's method — account snapshot, per-campaign verdicts, breakdown-effect-safe diagnosis, creative fatigue scoring, relevance/auction diagnostics, benchmarks, anomaly checks, competitor Ad Library research — then turns findings into ready-to-say commands. Use for ANY question about how Meta campaigns are doing, why a number moved, what to fix, which creative to run next, or what competitors run, for Delta Cuisine, Brico Kitchen or Ceram Quartz. Read-only by itself; changes go through era-ads-manager's manage rules.
---

# Era Meta Analyst — in-house, free, read-only

Built 2026-09-25 because Karim will not pay for HYPD or Adspirer: their value is
the *method*, and the *data* is free through Meta's own connector. This skill is
that method. Brand rules, account lock, page lock and write rules live in
`era-ads-manager` — this skill never overrides them.

## Data source — only these (all free)

Meta's official connector, `mcp__facebook-ads__*`:

| Need | Tool |
|---|---|
| Campaigns / ad sets / ads + metrics + breakdowns | `ads_get_ad_entities` (verify field names with `ads_get_field_context` first) |
| Trend over days, what changed | `ads_insights_performance_trend` |
| Sudden spikes / drops | `ads_insights_anomaly_signal` |
| How we compare to similar advertisers | `ads_insights_industry_benchmark`, `ads_insights_auction_ranking_benchmarks` |
| Account context (vertical, history) | `ads_insights_advertiser_context` |
| Meta's own improvement suggestions | `ads_get_opportunity_score` |
| Delivery errors, rejected ads | `ads_get_errors` |
| Who changed what, when (edits reset learning) | `ads_account_get_activity_logs` |
| Creatives, their ads, previews | `ads_get_creatives`, `ads_get_creative_ads`, `ads_get_ad_preview` |
| Competitors' live ads | `ads_library_search` |
| Meta's definition of a metric / policy | `ads_get_help_article` |

Published posts (to recommend what to boost next): Composio `FACEBOOK_GET_PAGE_POSTS`,
`INSTAGRAM_GET_IG_USER_MEDIA`. Never call paid plugins (HYPD, Adspirer) — even if
they appear in the tool list.

Tool parameters change; read each tool's schema before the first call in a run,
never guess a field name. If `ads_get_ad_entities` returns `next_actions`, run
the read-only ones before answering (connector contract).

## Step 0 — scope it right (every run)

1. **Account:** the brand's `meta_ad_account_id` from the brief, passed
   explicitly. Nothing else, ever — the connector also sees look-alike accounts
   ("DELTA CUISINE GROUP", "Conseiller Delta", …).
2. **Brand by Page:** in the shared Delta/Brico account, keep only campaigns
   promoting the brand's own page; the other brand is one "shared wallet" line.
   Read the page from each ad's creative: `ads_get_creatives` with fields
   `object_story_id` / `effective_object_story_id` — the part before `_` is the
   Page id (Delta `115558078454323`, Brico `100288952367054`). Only if that is
   empty, fall back to the ad copy / phone number, and say you did.
3. **Window:** default last 7 complete days (`last_7d`) **plus** lifetime for
   context. Say the window in the answer.
4. **Currency:** as Meta returns it (Delta/Brico EUR, Ceram Quartz USD).

## Step 1 — the account snapshot (always first)

One table, one row per campaign that spent in the window:
name · objective · status · daily budget · end date · spend · results · cost per
result · reach · frequency · CTR (link) · CPM · verdict.

- **Result = the campaign's own optimisation event** (conversations for
  messaging, reach for awareness, profile visits for followers, link clicks for
  traffic). Never judge a campaign on another campaign's metric.
- **Non-additive metrics:** reach and frequency cannot be summed across ad sets
  or campaigns (people overlap). For totals, request them at the higher level.
  Cost per result for a group = total spend ÷ total results, never an average
  of averages.
- Share of spend per campaign — a finding on a campaign with < 5% of spend is a
  footnote, not a headline.

## Step 2 — verdict per campaign

Use era-ads-manager's scale: SUCCESS / ON TRACK / TOO EARLY / NEEDS CHANGES /
FAILING, against the brand's own baselines (brands.yaml `ads.baselines` when
present, otherwise the account's own best comparable campaign). TOO EARLY when:
< 3 full days, < ~50 results in the week, or an edit in the last 3 days
(check `ads_account_get_activity_logs`).

## Step 3 — diagnose (only NEEDS CHANGES / FAILING), in this order

1. **Delivery:** `ads_get_errors`, ad review status, "with issues", paused
   parents, post no longer promotable, budget too low for the audience.
2. **Learning / edits:** a significant edit in the last 3–7 days explains
   volatility — say so and wait.
3. **Auction & relevance:** quality / engagement / conversion rankings
   (`ads_insights_auction_ranking_benchmarks`). Below-average quality → the
   creative or post; below-average conversion → the offer or the destination
   (slow replies count).
4. **Creative fatigue** (per ad, 7-day window vs the ad's first 7 days):
   | Score | Signal |
   |---|---|
   | Healthy | frequency < 2.5, CTR within 20% of its start |
   | Watch | frequency 2.5–3.5 or CTR down 20–30% |
   | Tired | frequency > 3.5 or CTR down > 30% or cost per result up > 30% |
   | Dead | Tired for 7+ days, or delivery collapsing |
   For video add **hook rate** (3-second views ÷ impressions; good ≥ 25%) and
   **hold rate** (ThruPlays ÷ 3-second views; good ≥ 15%) — low hook = first
   2 seconds; good hook + low hold = middle of the video.
5. **Audience saturation / overlap:** frequency climbing with flat reach; two of
   our campaigns on the same audience bidding against each other.
6. **Placement / age / region breakdowns — read with the breakdown effect in
   mind.** Meta spends where the NEXT result is cheapest, not where the average
   is cheapest; a segment with a high *average* cost may still be delivering
   the cheapest *marginal* results. So a breakdown is a **hypothesis**, never
   proof. Only act on it when the gap is large (> 2×), the segment holds a big
   share of spend (> 30%), and the data is meaningful (≥ 20 results). Even then,
   the recommendation is a controlled test, and the re-check date is part of it.

## Step 4 — benchmarks & anomalies (when asked "is this good?")

- `ads_insights_industry_benchmark` for the vertical, but the brand's own history
  outranks the industry — say both.
- `ads_insights_anomaly_signal` / `ads_insights_performance_trend` to answer
  "why did X change" with the date it moved and the likely trigger (edit in the
  activity log, budget change, new ad, end of an offer).

## Step 5 — recommendations (the only levers)

Real levers: campaign or ad-set **budget**, **pause/resume**, **end date**,
**new ad from another published post**, **new ad set with ONE changed variable**,
**placements/audience edit** (restarts learning — last resort). A breakdown is
not a lever. Each recommendation must carry:
- the evidence number, the expected effect, the money math (per day and total
  through the end date), the re-check date;
- a **ready-to-say command** naming the campaign and exact change
  ("raise the Interactions 21 Sep budget to 7.50 a day");
- pass the guardrails: never cut a campaign that is < 3 days old or in fresh
  learning; scale ≤ +25% per step; never pause the only campaign delivering
  a brand's results without naming the replacement.

## Step 6 — creative & competitor research (on request)

- **Own creatives:** rank ads by cost per result and hook rate; describe the
  winners (format, first 2 seconds, offer, CTA, language) so organic content can
  copy the pattern. Suggest the next post to boost from the Page's published
  posts with the best organic engagement.
- **Competitors:** `ads_library_search` with Tunisia, the category words
  (cuisine, dressing, quartz, plan de travail, électroménager) and known
  competitor pages. For each: format, hook, offer, CTA, how long it has run
  (long-running = probably profitable). Verdict per ad: emulate / selective /
  skip. Never copy a competitor's price or claims into our ads.

## ROAS

Meta sees no purchases for these brands. Report cost per result; give an
**estimated ROAS** only from Karim's own numbers (sales closed from ad
conversations × average sale value ÷ spend) and ask for them once per report.

## Output

Spoken summary first (≤ 6 sentences: headline, best, worst, the one action),
then the snapshot table, then diagnosis and commands. Save the full report as a
dated markdown in `<project_dir>\ads_reports\`. Never invent a number: a metric
that did not come back is written "not returned", with the reason.
