# FABLE META — Era Ads Manager plan (Meta Ads for Delta Cuisine & Ceram Quartz)

> Saved 2026-07-07 to proceed later. Approved decisions + full implementation plan.
> STATUS 2026-07-07: Facebook-side authorization DONE — the "ads MCP server" app is active on the user's Facebook account (user ID 27933146499604597) and connected to Claude (claude.ai freelance account). Remaining before execution: register the endpoint in Claude Code (`claude mcp add --transport http facebook-ads https://mcp.facebook.com/ads`) + one-click `/mcp` OAuth, then verify both ad accounts.
> Companion reading: `d:\telechargements\Claude_Agency_OS_Guide.pdf`, `d:\telechargements\claude-instagram-guide.pdf`
>
> **STATUS 2026-07-20:** Step 1's mechanical half is done — `facebook-ads`
> (`https://mcp.facebook.com/ads`) is now registered in
> `.claude-freelance\.claude.json`'s top-level `mcpServers` (backed up first:
> `.claude.json.bak-20260720_144005-preFBads`). **The `/mcp` OAuth step is still
> outstanding and must be done interactively by Karim** — an agent cannot click
> through Facebook's consent screen. A fresh check of both ad accounts (via the
> Meta Ads connector already available in this Claude.ai session) reconfirmed
> Ceram Quartz's account cleanly (`act_1698581228193609`, named "ceram quartz",
> business "Ceram Quartz") but found a **problem with Delta Cuisine**: the
> `act_33183820` this plan picked has no name and no business attached, while a
> separate, clearly-labeled `act_1033832265075508` ("DELTA CUISINE GROUP",
> business "DeltaCuisine") is also active — flagged as unresolved rather than
> guessed. Also found: Delta Cuisine has a THIRD, DISABLED ad account
> (`1054002982754860`, "DELTA cuisine Côte D'Ivoire", flagged by Meta for
> unusual activity) — different business, different market; never select this
> one regardless of which DC account query returns it.
>
> **RESOLVED same day:** Karim confirmed directly — **`act_33183820`
> (fable_meta.md's original 2026-07-07 pick) is correct.**
> `act_1033832265075508` ("DELTA CUISINE GROUP") is explicitly NOT it, despite
> looking like the better-labeled account; recorded in `brands.yaml →
> delta_cuisine.ads.meta_ad_account_excluded` so nothing selects it later on
> the strength of its name alone. **Budget is also resolved as a standing
> behavior, not a value to collect once:** there is no daily/lifetime cap to
> configure — the CM asks Karim for the end date and the budget on **every**
> campaign (`brands.yaml`'s `ads.budget_policy: ask_every_campaign`; procedure
> in `.claude/skills/era-ads-manager/SKILL.md`, "Budget — always asked, never
> assumed"). Step 2's data and Step 4's skill were fully built same day.
>
> **STATUS 2026-07-20 (later same day) — OAuth done, Step 5 built:** Karim
> completed the one-time `/mcp` OAuth approval in a session under
> `.claude-freelance`. Along the way, discovered an important distinction: the
> `/mcp` panel showed an ALREADY-CONNECTED "claude.ai Facebook" entry (used
> earlier this session for read-only account lookups) — but a live headless
> probe (`claude -p` under `.claude-freelance`) found **zero** Facebook tools
> reachable through it. That interactive-only connector does not carry into
> headless runs at all; only the explicitly-registered `facebook-ads` MCP
> server does. After OAuth, the SAME headless probe confirmed
> `mcp__facebook-ads__*` tools — including `ads_create_ad_set`,
> `ads_create_custom_audience`, `ads_activate_entity` — genuinely reachable.
> `ads.enabled` flipped to `true` for both brands. **Step 5 (dispatch wiring)
> is built and tested**: `main.py`'s `_ADS_INTENT`/`_infer_cm_task_type`,
> `era_dispatch.py`'s `_build_ads_brief()` + `run_task()`'s enabled-gate and
> freelance-account override, `_DEFAULT_ALLOWED_TOOLS["ads"]`. 17 tests in
> `tools/test_ads_dispatch.py`, all passing; full regression suite (94 tests)
> also passing. **Steps 6–7 (reporting pipeline, real dry-run campaign) still
> NOT started** — the wiring is verified by tests and a tool-availability
> probe, not by an actual campaign creation yet.
>
> **Also resolved 2026-07-20, unrelated to Ads:** Karim rejected using
> Magnific's `audio_music_generate` for Reel background music ("use anything
> else"), then confirmed Google AI Studio / Lyria as the replacement and
> supplied a dedicated `gemini_music_api_key`. Built as `music_generate.py`
> (repo root) — calls Google Lyria RealTime directly via the `google-genai`
> SDK, isolated from Era's voice-quota key. **Live-tested same day**: real
> 8s and 6s tracks generated and verified with ffprobe (correct codec/rate/
> channels/duration). Needed `http_options={"api_version": "v1alpha"}` on the
> client — the default v1beta 404s the endpoint outright; not discoverable
> from the SDK's method signatures, only from a live probe. See
> `obsidian_era/era_brain/Resources/Automated_Reel_Assembly_Pipeline.md` for
> full pipeline usage and `tools/test_music_generate.py` for the offline test
> suite (key-resolution precedence, mime-type parsing, WAV/ffmpeg transcode,
> CLI wiring — everything except the live call itself, which is a manual
> opt-in test via `GEMINI_MUSIC_API_KEY`).

## Decisions already made (do not re-ask)

| Topic | Decision |
|---|---|
| Connector | **Official Meta Ads MCP** `https://mcp.facebook.com/ads` (free, first-party). Windsor rejected (subscription). Already connected in the **freelance claude.ai account** — but NOT bridged into Claude Code, so it must also be registered in Claude Code (see step 1). |
| Second Claude account | **Not needed.** No Facebook connector on the Delta Cuisine Claude account — media buying is centralized in the freelance account; brand separation is enforced by per-brand ad accounts + Brand Lock. |
| Delta Cuisine ad account | **act_33183820** |
| Ceram Quartz ad account | **act_1698581228193609** |
| Audiences | Build **"Claude DC Audience"** and **"Claude CQ AUDIENCE"** (specs below, derived from the two brand reports). |
| Review gate | **Everything created PAUSED.** Present review summary, stop. Activate only after explicit user approval, never in the same run. |
| Scope | Full: skill + brands.yaml + era_dispatch/hermes_router voice orders. |
| Reports | Performance report as **PDF emailed to each brand's `notify_email`** (copy kept in brand `project_dir`). Findings feed back into the CM (learning loop). |

## Step 1 — Register Meta Ads MCP in Claude Code
- `claude mcp add --transport http facebook-ads https://mcp.facebook.com/ads` at **project scope** (`.mcp.json`, next to context7) so headless `claude -p` runs can use it too.
- User authenticates once via `/mcp` (OAuth through Facebook's own consent screen — official API, no ToS/blocking risk; actions auditable in Ads Manager activity history).
- Verify both ad accounts visible: act_33183820 (DC) and act_1698581228193609 (CQ).
- OAuth tokens live per Claude config dir → **ads tasks always run under the freelance/default config**, never a brand's `claude_config_dir`.

## Step 2 — `brands.yaml`: `ads:` block per brand — DONE 2026-07-20

Built as planned, with two corrections from what's below: DC's account is
confirmed (not TBC) with an explicit exclusion list for the look-alike account,
and budget is a standing per-campaign question, not a config value. Actual
shape now in the repo:

```yaml
# under brands.delta_cuisine
ads:
  enabled: false                    # true once /mcp OAuth is done
  meta_ad_account_id: "act_33183820"          # confirmed by Karim 2026-07-20
  meta_ad_account_excluded:
  - {id: "act_1033832265075508", name: "DELTA CUISINE GROUP", ...}  # NOT this one
  page_id: "115558078454323"
  default_objective: OUTCOME_LEADS      # premium custom kitchens → lead/DM driven
  currency: TND
  audiences: [{name: "Claude DC Audience"}]
  budget_policy: ask_every_campaign     # ask end date + budget EVERY campaign, no standing cap
  review_gate: always_paused

# under brands.ceram_quartz
ads:
  enabled: false                    # true once /mcp OAuth is done
  meta_ad_account_id: "act_1698581228193609"  # confirmed, no conflict
  page_id: "788466647689882"
  default_objective: OUTCOME_LEADS      # sales close via DM/WhatsApp/phone
  currency: TND
  audiences: [{name: "Claude CQ AUDIENCE"}]
  budget_policy: ask_every_campaign
  review_gate: always_paused
```

## Step 3 — The two named audiences (from real report data)

Create as **saved audiences** in each ad account with these exact names (via Meta MCP if it exposes saved-audience creation; otherwise store the spec in brands.yaml and apply at ad-set level). Present final spec to user with the first paused campaign.

**"Claude DC Audience"** — source: `D:\Ctrl Z\Freelence\DC\IG\ABC\claude automation\graphify-out\converted\rapport_instagram_delta_cuisine_f44c6f76.md`
Report insight: audience responds to aspiration/storytelling (luxury kitchens, dressing sur mesure: 100+ engagements vs 10–25 for price-led posts); Tunisia; bilingual FR/AR; ~50k followers.
- Geo: Tunisia (weight Grand Tunis + major cities) · Age 28–55 · Languages FR + AR
- Interests: kitchen design, interior design, home renovation, custom furniture, luxury home; homeowners/recently-moved behaviors where available
- Retargeting layer (highest intent): FB page + IG engagers 365 days

**"Claude CQ AUDIENCE"** — source: `D:\telechargements\ceramequartz\new\Rapport_Strategique_Instagram_CeramQuartz.md`
Hard data: 62.8% women · 25–44 = 70.7% · Tunisia = 94.8% · CTA/promo posts with price+phone drive action · carousels = best format.
- Geo: Tunisia · Age 25–44 core (extend to 54) · women-weighted, men not excluded
- Interests: plan de travail/countertops, quartz, ceramics/marble/granite, kitchen renovation, bathroom remodel, construction; secondary layer: contractors/kitchen installers (B2B)
- Objective geared to sales conversations: leads/messages (WhatsApp/DM/call)
- Retargeting layer: page + IG engagers 365 days

## Step 4 — New skill: `.claude/skills/era-ads-manager/SKILL.md`
Flat single file, `name:` + `description:` frontmatter, mirroring `interior-design` ("brands.yaml is authoritative; if they disagree, brands.yaml wins"). Content:

- **From Agency OS guide (Media Buying + Reporting departments):** 3 copy variations per campaign (pain-led / outcome-led / social-proof-led); field limits (primary 125 / headline 27 / description 27 chars); FR for DC, FR + Tunisian AR for CQ (reuse `caption_footer` / `caption_footer_ar`); targeting hypotheses with rationale; A/B variable isolation; report framework = headline result → 3 wins → 1 improvement area → 2–3 recommendations.
- **From claude-instagram-guide (deep audit method):** normalized score per ad/creative (CTR, CPC, results, frequency), one-line diagnosis tags, **DO MORE / STOP / FIX** action grid.
- **Hard safety rules:** Brand Lock (always pass explicit `meta_ad_account_id`, never default account, never `meta_ad_account_excluded` — same trap as CQ's Composio IG); ALWAYS create paused; **ask Karim for the end date and the budget on every single campaign** (no standing cap — `budget_policy: ask_every_campaign`), then compute + confirm total spend implied before creating anything.

## Step 5 — Era dispatch integration (voice/CLI orders) — DONE 2026-07-20
- `era_dispatch.py`: `_DEFAULT_ALLOWED_TOOLS["ads"]` allowlists `mcp__facebook-ads` + `mcp__composio`; `run_task()` overrides `claude_config_dir` to `defaults.ads_claude_config_dir` (freelance) for every ads task regardless of brand, and refuses when `ads.enabled` is false; `_build_ads_brief()` is a dedicated brief (not injected into the organic one — see its docstring for why) built from `brands.<key>.ads`.
- `main.py`: `_ADS_INTENT` regex + `_infer_cm_task_type`, checked before `_CAMPAIGN_INTENT` so organic vs. paid "campaign" don't collide; `community_manager` tool's task_type validation tuple includes `"ads"`.
- `hermes_router.py`: `_CM_KEYWORDS` extended with "ads", "publicité", "sponsoring", "boost", "ad budget", "campagne sponsorisée" (closes the one gap where a bare budget question has no other CM-recognized word).
- Test coverage: `tools/test_ads_dispatch.py` (17 tests — intent classification, brief separation/content, account-lock/exclusion rendering, budget-ask mandate, cross-brand isolation, the `ads.enabled` gate, the freelance config-dir override, allowed-tools).
- `docs/CM_COMMANDS.md`: ads order phrasing documented.

## Step 6 — Reporting → PDF → learning loop
- Pull performance via Meta MCP insights tools → analysis per skill framework → HTML → PDF (Python) → save to brand `project_dir` → **email to brand `notify_email`** (Gmail/Composio).
- Append validated learnings (winning hooks, audiences, times, formats) to the
  brand's vault Areas note (`core/cm_vault.py`'s `append_run_log`, or a
  dedicated ads-findings note) — **not** `memory_dir`, which is dead config
  (nothing reads it; corrected across the repo 2026-07-20). Surface findings to
  the community manager so organic content reuses what paid data proved.

## Step 7 — Dry-run verification
- One test campaign per brand, tiny budget, created PAUSED; user checks Meta Ads Manager: correct ad account, nothing live.
- Insights tools return campaign rows; PDF renders; email arrives.
- `python -m ruff format .` · `ruff check --fix .` · `pytest tools/ -q` after the Python edits.

## Still needed from user at execution time

None currently. OAuth is done (confirmed reachable headless), both ad
accounts are confirmed, and budget is handled per-campaign by asking, not by
a standing config value. What's left is Steps 6–7 (reporting pipeline,
one real dry-run test campaign) — implementation work, not something blocked
on Karim.
