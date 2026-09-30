---
name: era-ads-manager
description: Meta Ads (Facebook/Instagram) rules for Delta Cuisine, Brico Kitchen (ads-only, shares Delta's ad account and card — tell campaigns apart by Page) and Ceram Quartz — performance playbook, published-posts-only creatives, copy formulas, targeting, the mandatory per-campaign budget question, and the always-paused review gate. Use BEFORE proposing, building, or reporting on any paid campaign for either brand. brands.yaml's `ads:` block is authoritative on account ids; if this skill and brands.yaml disagree, brands.yaml wins. LIVE as of 2026-07-20 — dispatch wiring built and verified, see "Current status."
---

# Era Ads Manager — Meta Ads for Delta Cuisine & Ceram Quartz

Single source of truth for routing numbers: `brands.yaml → brands.<key>.ads`.
This skill is the reasoning procedure that turns those numbers into good
campaigns. Full history and original decisions: `fable_meta.md` (repo root).

## Current status — LIVE (confirmed 2026-07-20)

`ads.enabled` is `true` for both brands. All three original gates are clear:

1. **MCP authenticated and verified headless.** The `facebook-ads` server is
   registered in the freelance Claude account
   (`.claude-freelance\.claude.json`, top-level `mcpServers`). Karim completed
   the one-time `/mcp` OAuth approval, and a live probe under that config dir
   confirmed `mcp__facebook-ads__*` tools — including create/activate ones —
   are actually reachable from a headless `claude -p` run, not just
   "connected" in the interactive `/mcp` panel. **Important distinction
   learned here:** an already-connected "claude.ai Facebook" entry showing in
   `/mcp` does NOT mean headless runs can use it — that's an interactive-only,
   account-level connector, separate from an explicitly-registered MCP server
   like `facebook-ads`. Only the latter carries into `claude -p`.
2. **Both ad accounts resolved.** `brands.yaml → <brand>.ads.meta_ad_account_id`,
   confirmed directly by Karim. Delta Cuisine also carries a
   `meta_ad_account_excluded` entry — a look-alike account Karim explicitly
   ruled out; never select it even if a lookup surfaces it as a plausible match.
3. **Budget resolved as a standing behavior, not a config value** — see
   "Budget — always asked, never assumed" below.

**Dispatch wiring is built and tested** (`tools/test_ads_dispatch.py`, 17
tests): `main.py`'s `_infer_cm_task_type` recognizes ads-flavored orders
("boost this post," "run a Meta ad campaign," "budget publicitaire") and
routes to `task_type="ads"`; `era_dispatch.py` has a dedicated
`_build_ads_brief()` (deliberately separate from the organic-content brief —
see its docstring) and `run_task()` enforces the `ads.enabled` gate and always
overrides `claude_config_dir` to the freelance account for ads tasks,
regardless of which brand, since that's the only account with the OAuth
token. **Not yet done:** a real end-to-end dry-run (Step 7 below) creating an
actual paused test campaign — the wiring is verified by tests and a headless
tool-availability probe, not by a real campaign creation yet.

## Brands and accounts (updated 2026-09-25) — never confuse them

| Brand | Ad account | Page | What Karim does |
|---|---|---|---|
| Delta Cuisine | `act_33183820` | `115558078454323` | full CM + ads |
| **Brico Kitchen** (Delta Cuisine group) | `act_33183820` (**shared**) | `100288952367054` | **ads only** — boosts already-published posts |
| Ceram Quartz | `act_1698581228193609` | `788466647689882` | full CM + ads |

- `act_33183820` bills in **EUR** (confirmed by Karim 2026-09-25). **Delta Cuisine
  takes the big share of that shared budget**, Brico Kitchen the smaller one —
  when the wallet is tight, protect Delta's best campaigns first.
- Delta Cuisine and Brico Kitchen share one ad account **and one payment card**.
  A campaign's brand = the **Page it promotes**, never its name ("Nouvelle
  campagne de Interactions" exists for both). Read the promoted page before
  touching or judging any object; the dispatcher's PAGE LOCK enforces this.
- Reports for one of them list only its own campaigns; the other brand's spend
  is one "shared wallet" line. Budget moves across the two only when the order
  names both.
- Brico Kitchen: never create, plan or publish content (another team does);
  `lanes: [ads]` refuses organic orders. Its posts carry dated offers — an ad
  never runs past the offer's end date.

## Hard safety rules (always, once live)

- **Brand Lock for ads.** Always pass the explicit `meta_ad_account_id` from
  `brands.yaml` — never let the tool default to whatever account is "current,"
  and never select an id listed under `meta_ad_account_excluded`. This is the
  exact same trap as Composio's default-IG-account issue that already burned
  this project once; ad spend makes the cost of that mistake real money, not
  just a wrong post.
- **Everything created PAUSED, no exceptions.** Campaign, ad set, ad — created
  paused, presented to Karim as a review summary, activated only after his
  explicit approval, and never in the same run that created it.

## Budget — always asked, never assumed (corrected 2026-07-20)

`brands.yaml`'s `ads.budget_policy: ask_every_campaign` is not a placeholder
waiting to be filled in once — **there is no standing budget cap.** Every
campaign has its own spend and its own end date, so the CM asks Karim for both
**every time**, before building anything:

1. **"When should this campaign end?"** — a date, or a duration ("run it for
   14 days").
2. **"What's the budget?"** — daily or total/lifetime, Karim's choice.

Given either form, **compute the other side and state the total spend implied**
before creating anything ("14 days at 20 TND/day = 280 TND total."). If the
order already gives both, that IS the answer — state the total and build; never
ask Karim to confirm numbers he just gave (2026-09-25).
Never infer a budget from a past campaign, a competitor, or a rule of thumb —
if Karim hasn't answered both questions for *this* campaign, the CM does not
create it yet, paused or not.

## Campaign construction

- **Creative = an already-published post** (Karim, 2026-09-25): find it on the
  Page / Instagram (Composio `FACEBOOK_GET_PAGE_POSTS`,
  `INSTAGRAM_GET_IG_USER_MEDIA`, `INSTAGRAM_GET_IG_USER_STORIES`) and build with
  `object_story_id` — full-resolution, and it keeps the post's likes/comments as
  social proof. Never upload a vault/project file or a re-downloaded story copy
  (that one ran at 720p). Product names are spelled loosely ("Tej Mahal").
- **Number of ads follows the budget** (see Performance playbook): at ≤ ~5/day
  run ONE ad (the published post as-is) or two at most. The pain / outcome /
  social-proof copy trio is for budgets that can feed ~50 results per ad set
  per week.
- **Field limits:** primary text 125 chars, headline 27, description 27.
- **Language:** French for Delta Cuisine; French **and** Tunisian Arabic for
  Ceram Quartz — reuse the brand's existing `caption_footer` /
  `caption_footer_ar` voice, don't invent a separate ad voice.
- **Targeting hypotheses need a stated rationale**, not just a demographic
  guess — tie each targeting choice to something in the brand's own
  performance data (the strategic reports already in each project_dir, or the
  vault's brand Resources notes).
- **A/B test one variable at a time** (creative OR audience OR copy, not all
  three at once) so a result is actually attributable.
- **Messaging ads: no automated greeting (Karim, 2026-09-19).** On any ad that
  opens Messenger/WhatsApp/Instagram DM, the customer types their own request.
  Turn OFF the "Conversations" template — welcome/introduction message and the
  suggested Questions et réponses — instead of accepting Meta's default
  ("Bonjour Karim ! Dites-nous comment nous pouvons vous aider" + 3 canned
  questions). Applies to both brands, every new messaging campaign or ad set.
  Existing live campaigns are only changed when Karim names them.

### Named audiences (from real report data — build these first)

**"Claude DC Audience"** — source: Delta Cuisine's IG performance report.
Audience responds to aspiration/storytelling (luxury kitchens, dressing sur
mesure) far more than price-led posts.
- Geo: Tunisia (weight Grand Tunis + major cities) · Age 28–55 · FR + AR
- Interests: kitchen design, interior design, home renovation, custom
  furniture, luxury home
- Retargeting layer (highest intent): FB page + IG engagers, 365 days

**"Claude CQ AUDIENCE"** — source: Ceram Quartz's strategic report.
62.8% women · 25–44 = 70.7% · Tunisia = 94.8% · CTA/promo posts with
price+phone drive action.
- Geo: Tunisia · Age 25–44 core, extend to 54 · women-weighted, men not excluded
- Interests: countertops/plan de travail, quartz, ceramics/marble/granite,
  kitchen/bathroom renovation, construction; secondary B2B layer:
  contractors/kitchen installers
- Objective: leads/messages (WhatsApp/DM/call)
- Retargeting layer: page + IG engagers, 365 days

## Reporting (once campaigns are live)

- Pull performance via the Meta Ads MCP's insights tools.
- **Normalized score per ad/creative**: CTR, CPC, results, frequency. One-line
  diagnosis tag per ad.
- **Action grid: DO MORE / STOP / FIX** — every ad gets exactly one bucket.
- **Report framework:** headline result → 3 wins → 1 improvement area → 2–3
  recommendations. Render to PDF, save in the brand's `project_dir`, email to
  `notify_email`.
- **Feed the learning loop.** Append validated findings (winning hooks,
  audiences, times, formats) to the brand's vault Areas note so organic
  content — the regular CM lane — reuses what paid data proved. Paid and
  organic should teach each other, not run as separate silos.

## Reporting and management — the senior CM playbook (added 2026-09-19)

Two extra modes of the `ads` lane, chosen from the order's wording by
`app.cm_intents.ads_mode()` — **report** (read-only) and **manage** (changes
existing campaigns). Anything else is **build** (the sections above). Each mode
has its own brief in `era_dispatch._build_ads_ops_brief`; they must not share
rules, because "always create PAUSED" and "change a live campaign" contradict.
Where this section and "Reporting" above disagree, this one wins: the report is
a dated markdown in `<project_dir>\ads_reports\`; PDF / email only when asked.

**Karim's goal (Ceram Quartz): grow Page + Instagram followers (reach NEW
people) and make sales.** Meta cannot see a closed sale — a message conversation
is the proxy, so ask once how many became quotes or showroom visits. The numbers
to judge against live in `brands.yaml → ceram_quartz.ads.baselines` (the
account's own history, USD): message conversation best $0.30 / typical
$0.43–0.51 / poor above $0.80; profile-or-page visit ~$0.03 / poor above $0.10.

### Judge each campaign against ITS OWN objective — one verdict

| Verdict | Means |
|---|---|
| SUCCESS | beats baseline on its goal, enough volume to trust it |
| ON TRACK | inside baseline, volume still thin |
| TOO EARLY | < ~50 results/week, < 3 full days, or still learning — say what to wait for and until when |
| NEEDS CHANGES | delivering but off baseline — name ONE main cause + the number that proves it |
| FAILING | not delivering, or far off baseline with enough data |

Never call an early campaign a failure. A follower/awareness campaign is judged
on cost per profile visit, reach cost and net new followers — never on messages;
a messaging campaign on cost per conversation and reply rate — never on reach.

### Diagnosis, in this order (stop at the first cause that fits)

1. **Not delivering** — ad rejected, billing/payment, budget lower than the
   audience needs, end date passed, or it is still an unpublished draft.
2. **Too early / learning** — do nothing; report when it will be judgeable.
3. **Audience** — reach fine but cost high: too narrow or overlapping another of
   our campaigns (they bid against each other); check the age/region breakdown.
4. **Creative** — high CPM or low CTR, frequency > ~3, falling CTR over days
   (fatigue): rotate or add a creative. Hook is in the first 3 s of a Reel.
5. **Copy / offer** — clicks but no conversations: promise mismatch, no price
   or reason to message. Never invent a price; use only the approved claims.
6. **Response time** — conversations start but go cold: replies slower than a
   few hours. The cheapest fix in the account is answering faster.
7. **Placement / schedule / device** — one placement eating spend at poor cost.

### Scenario → recommended action (say it as a complete command)

| Situation | Recommendation |
|---|---|
| SUCCESS, budget-limited | raise budget ≤ +25% every 2–3 days |
| SUCCESS, time-limited | extend the end date (cheaper than a rebuild, keeps learning) |
| TOO EARLY | wait; name the date to re-check |
| NEEDS CHANGES: creative fatigue | new ad (never edit the live one), pause the tired one |
| NEEDS CHANGES: audience | test ONE changed audience as a new ad set — one variable at a time |
| Follower campaign works, sales weak | move budget toward the messaging campaign; keep followers as the top of funnel |
| Two campaigns overlap | give one the audience, pause or re-target the other |
| FAILING | pause, keep the learnings in the vault, rebuild small |

Every recommendation is a one-line command that stands alone (campaign name +
exact change), because the next order reaches a run with no memory of this one:
"extend the SAVANA campaign to 30 Sep", "raise the CQ Ventes budget to $5/day".

### Manage — what a run may do, and the one gate

Allowed on the named campaign: extend/shorten the end date, raise/lower a budget,
pause/resume, duplicate (the copy is PAUSED), rename, change locations/age/
audience, add a NEW ad or ad set (PAUSED). Never edit a running ad's creative in
place.

- A **complete command** (campaign + exact change) IS the confirmation — act.
- A **vague** one ("scale the winner") is NOT — show the exact change and the
  money math, stop, and hand back the command to say.
- Pauses and reductions never need confirmation once the target is clear.
- Before any increase state: current → new per day, added spend per day, TOTAL
  added through the end date, new total commitment.
- Budget rises above +25% at once (+35% in a week) can reset learning: warn if
  Karim asks for it, cap at +25% if the run is choosing.
- Verify every change by reading it back from Meta; report before → after.
- The account holds many unrelated unpublished drafts. Never use
  "review and publish" / publish-all — edit only the named object.

Ways Karim can drive it (any wording; the lane infers the mode): "how are my
campaigns doing", "analyse the SAVANA campaign", "extend it by 3 days", "raise
the budget to $5/day", "pause the followers campaign", "expand the audience to
Sfax and Sousse", "duplicate the winning ad set". To get it unprompted, ask Era
to schedule it ("every morning at 9 report my ads") — the scheduler routes that
order into the report mode.

## Tools — which one for which job (updated 2026-09-25)

| Job | Tool |
|---|---|
| **Every change** — create, edit, pause, budget, dates, placements, activate | `facebook-ads` (Meta's official MCP — free; Brand/Page Lock verified on it) |
| **Every analysis** — reports, audits, why-did-it-change, fatigue, benchmarks, competitor ads | `facebook-ads` read tools, run through the in-house **`era-meta-analyst`** skill (`.claude/skills/era-meta-analyst/SKILL.md`) |
| Published posts to boost | Composio `FACEBOOK_GET_PAGE_POSTS` / `INSTAGRAM_*` |

**Never use paid plugins** (HYPD, Adspirer, …) — Karim won't pay for them; the analyst
skill is the free replacement, and they are not on the ads allowlist.

## Performance playbook — lowest cost per result (added 2026-09-25)

The goal is the lowest cost per result on each campaign's OWN objective and,
when sales data exists, the highest ROAS. These are the levers, in the order
they usually pay off for two small-budget Tunisian brands.

### Budget and structure — feed the algorithm, don't split it
- Meta needs ~**50 results per ad set per week** to leave the learning phase.
  Minimum sensible daily budget per ad set ≈ (target cost per result × 50) ÷ 7.
  At $0.40/conversation that is ~$3/day for ONE ad set; each extra ad set or ad
  splits that and slows learning.
- **Consolidate:** one campaign per objective, one ad set per real audience,
  1–2 ads. Prefer campaign-level budget (Advantage campaign budget) when a
  campaign has several ad sets, so money flows to the cheaper one automatically.
- Never run two of our own campaigns at the same audience with the same
  objective — they bid against each other and raise CPM. Check overlap before
  building; if found, merge or exclude.
- Scale winners ≤ +20–25% per step, every 2–3 days. Cut losers fast: lowering
  or pausing never needs a waiting period.

### Audience — broad beats clever in a small market
- Tunisia is a small auction. **Advantage+ audience (broad + suggestions)**
  usually beats narrow interest stacks on cost per result. Use the named
  audiences as *suggestions*, not hard limits, unless a breakdown proves a
  segment is expensive.
- **Retargeting is the cheapest conversation**: Page + IG engagers (365 d),
  video viewers (50%+), people who messaged before. Give messaging campaigns a
  retargeting ad set when volume allows.
- **Awareness / follower campaigns: exclude existing followers/engagers** — we
  pay to reach NEW people.
- Trim with data, not guesses — and mind the **breakdown effect** (Meta buys the
  cheapest *next* result, so a segment's high *average* cost can still hide cheap
  marginal results). A segment is a candidate only when it costs > 2× the average,
  holds > 30% of spend and ≥ 20 results; the change is a test with a re-check date
  (see `era-meta-analyst` Step 3.6).

### Placement and format
- Default: Advantage+ placements. For messaging objectives **exclude Audience
  Network** (cheap accidental clicks, no conversations).
- Vertical 9:16 video is native to Reels/Stories, where CPMs are lowest.
  The hook is the first 3 seconds; the product must be on screen by second 2.
- Set the Instagram identity (`instagram_user_id`) when one is available; with
  no IG account linked to the ad account, Instagram delivery runs under the
  Page identity and is weaker — tell Karim once, don't block on it.

### Optimization goal and bidding
- Match the goal to the business result: messaging → **Conversations**
  (WhatsApp / Messenger / IG Direct — test which one the brand's customers
  answer on), awareness → Reach, followers → profile/Page visits.
- Bid strategy: **Highest volume (lowest cost)** by default. Only use a cost
  cap once an ad set has a stable history, set ~10–20% above its 7-day average.

### Creative
- The best ad is usually the best-performing organic post (proof: engagement).
  Rank candidate posts by engagement rate before choosing.
- Refresh when frequency > ~3 (7 days) or CTR falls ≥ 30% from its first week:
  add a NEW ad from another published post, pause the tired one.

### Learning phase discipline
- Don't touch an ad set in learning (< 50 results, < ~3–7 days) unless it is
  not delivering or clearly broken. Significant edits (audience, goal,
  creative, budget > ±25%) restart learning.

### Optimizing an underperformer — the procedure
1. **Confirm it's real:** ≥ 3 days, enough spend (≥ ~10× target cost per
   result), out of learning. Otherwise: TOO EARLY.
2. **Find the one cause** with breakdowns (age, gender, region, placement,
   platform position, ad): costs usually concentrate in 1–2 segments.
3. **Fix that one thing** — exclude the expensive segment / Audience Network,
   pause the weakest ad, swap in a stronger published post, fix the message
   destination or reply speed. One change at a time, so the result is readable.
4. **Set the re-check date** (3 days after the change) and the success bar
   (e.g. cost per conversation ≤ account average).
5. **If it fails the bar:** lower its budget and move the difference to the best
   campaign with the same objective, in ≤ +25% steps on the receiver. State the
   money math. Never move more than the receiver can absorb without its cost
   per result rising.

### ROAS — be honest about what Meta can see
- Neither brand sends purchase events: Meta cannot compute ROAS, only cost per
  conversation / visit / reach. Never present a ROAS number Meta didn't report.
- **Estimated ROAS** only from Karim's own numbers: (sales closed from ads ×
  average sale value) ÷ ad spend for the same period. Ask for those numbers
  once per report; record answers in the brand's vault note.
- To make ROAS real later: log sales against the conversation (Meta business
  messaging purchase events / Conversions API), or at minimum a weekly count
  of quotes and sales that came from ad conversations.

### Currency
Report amounts in the currency **Meta returns for that account** (the Delta
Cuisine account reports in EUR, Ceram Quartz in USD), even if brands.yaml says
otherwise — say which one, never convert silently.

### Field-verified traps (Ads Manager, 2026-09-19)

- **Duplicating the old "Ventes" (Sales objective) silently changed its
  conversion location to "Website"** — a copy that optimises for page views. Do
  not duplicate that campaign; build an Engagement → Messaging one instead
  (the setup behind the account's best result, $0.30 per conversation).
- **Meta defaults a new campaign's budget to $115/day.** Always overwrite it and
  read back "Nous dépenserons environ X $ par jour" before going on.
- **The post picker preselects a different, older post.** Confirm the attached
  post's text; an old saved template and an old post both carried a "330 DT"
  price that must never go out again.
- **The facebook-ads MCP's OAuth token dies and cannot renew itself** (found
  2026-09-20: expired 18 Sep, and the stored entry has no refresh token). Every
  headless run after that reached Meta, got nothing back, and produced a report
  of "[NOT PULLED]" — 20 minutes and $10.84 for nothing. `run_task` now checks
  this first (`era_dispatch._ads_connection_problem`) and answers in under a
  second. Fix is manual and only Karim can do it: run `claude` under
  `C:\Users\nesri\.claude-freelance`, type `/mcp`, choose facebook-ads,
  Authenticate. Expect to repeat it; the check will say when.
- **Read-only reports skip the one-at-a-time queue** (a report needs neither
  Magnific nor Adobe); asking twice joins the report already running. Report and
  manage runs carry a `--max-budget-usd` cap (`_ADS_OPS_MAX_BUDGET_USD`).
- **An ad-set edit re-validates every ad under it (2026-09-25).** Delta's
  "packs infinity" ads were delivering on a Brico Kitchen Page post that had
  since become unusable ("post cannot be used in an ad / invalid, possibly
  deleted"). Removing Facebook Feed made Meta force-pause and re-check the ad
  set, both ads failed, and the campaign stopped delivering. **Before editing a
  live ad set, read each ad's `issues_info` / promoted post; if the post is dead,
  build the replacement ad (new valid published post, PAUSED) first, then edit.**
  After any edit, read back effective status — "force-paused" and "with issues"
  mean zero delivery.
- **Meta rewrites placements on duplicate** and shows the extension-warning
  popup after most edits; neither is an error.
- Editing a live campaign's ad opens an editor that stamps "unpublished changes"
  on the campaign, ad set and ad — discard those drafts (per object) or the
  account's pending-drafts counter grows and publish-all becomes dangerous.

## How this plugs into a run — built 2026-07-20

- `main.py`'s `_ADS_INTENT` regex + `_infer_cm_task_type` route ads-flavored
  orders to `task_type="ads"`, checked before the organic `_CAMPAIGN_INTENT`
  so "run a promo campaign" (organic) and "run a Meta ad campaign" (paid)
  don't collide.
- `era_dispatch.build_brief(task_type="ads")` calls `_build_ads_brief()` — a
  **separate** brief from the organic one (not a bolted-on section), because
  the organic brief's "never stop to ask" instruction directly contradicts
  the mandatory per-campaign budget question here. It states the Ad Account
  Lock (with excluded ids), the budget-ask rule, always-paused, and the
  brand's named-audience spec from `brands.yaml → ads.audiences`.
- `era_dispatch.run_task()` refuses to dispatch when `ads.enabled` is `false`
  for that brand, and always overrides `claude_config_dir` to
  `defaults.ads_claude_config_dir` (the freelance account) for `task_type="ads"`
  — regardless of which brand — since that's the only account with the
  `facebook-ads` OAuth token. `_DEFAULT_ALLOWED_TOOLS["ads"]` allowlists
  `mcp__facebook-ads` and `mcp__composio` (for the report-email step), not
  `mcp__magnific` — ads reuses already-produced organic assets.
- Test coverage: `tools/test_ads_dispatch.py`.
