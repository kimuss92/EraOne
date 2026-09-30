# Studio CM — Era One Operating Workflow

> How Era One (voice + text agent) uses Claude to run community management for
> **multiple brands** like a senior CM would — from brief to published post —
> without ever posting to the wrong page or drifting from a brand's charter.

---

## 0. The mental model

```
  YOU (voice or text)
      │
      ▼
  ERA ONE  ──────────────►  the front door. Understands intent, extracts the
  (voice/text agent)        BRAND + the TASK, hands a structured brief to Claude.
      │
      ▼
  CLAUDE  ────────────────► the senior CM brain. Plans, researches, writes,
  (intelligence + tools)    orchestrates production, routes publishing.
      │
      ├── BRAND REGISTRY (brands.yaml) ── the single source of truth for routing
      │                                    (handles, account IDs, folders, times)
      ├── BRAND CHARTER (per-brand doc)  ── the visual + tone rulebook
      │        e.g. Delta Cuisine = MASTER_CONTENT_BLUEPRINT.md
      ├── BRAND MEMORY (per-brand folder) ── past posts, what performed, decisions
      │
      └── TOOLS (the hands)
           ├─ Adobe (design / video edit)      ├─ Magnific Space (AI kitchen/product visuals)
           ├─ Composio (Instagram + Facebook + Gmail; TikTok as it's connected)
           ├─ Zernio (legacy — retired for current brands, kept as an option)
           └─ ffmpeg / Python (assembly, music, overlays)
```

**Two rules that make the whole thing safe:**

1. **Nothing is hardcoded to a brand.** Every task is *brand-scoped*. Claude reads
   the brand's registry entry + charter *before* it does anything.
2. **Two approval gates, and Claude decides everything in between.** Claude picks the
   material, texture, hook, composition and copy itself — you never answer "quartz or
   ceramic?". **Gate 1:** you approve the plan, per item. **Gate 2:** you approve each
   *finished asset*, because a render can come out wrong even from a plan you liked.
   A plan approval is never an asset approval. Nothing outside the approved plan is
   produced, nothing unapproved is published, and the Brand Lock fences *where* it lands.

---

## 1. The Brand Key — how Era One never mixes brands up

Every command Era One sends to Claude must resolve to exactly one **brand key**
(e.g. `delta_cuisine`, `ceram_quartz`). Era One resolves it from what you say:

- *"Plan Delta Cuisine's week"* → `delta_cuisine`
- *"Make a promo campaign for Ceram Quartz"* → `ceram_quartz`
- If the brand is ambiguous or missing → **Era One asks before doing anything.**
  It never guesses.

Once resolved, Claude runs a **Brand Lock** (see §6) that echoes the brand back to
you before producing or publishing. This is the seatbelt against wrong-page posts.

---

## 2. The two request types

| Type | Trigger example | Cadence | Output |
|------|------------------|---------|--------|
| **A — Weekly Plan** | *"Prepare this week's content for Delta Cuisine."* | Recurring (per brand) | Calendar + reels + carousels for the week |
| **B — Campaign** | *"Build a promo campaign for Ceram Quartz's summer sale."* | On demand | Concept + hero reel + carousels + landing captions, all themed to the offer |

Both run through the **same 9-phase pipeline** below. A weekly plan is just a
campaign with the theme = "the brand's normal content mix for this week."

---

## 3. The senior-CM pipeline (both request types run this)

```
 0 INTAKE ─► 1 RESEARCH ─► 2 STRATEGY ─► 3 PLAN ─►【GATE 1: you approve the plan,
                                                     item by item, or name a fix】
                                                          │
                                    ┌── per plan-approved item, one at a time ──┐
                                    │  4 PRODUCE ─► 5 QC ─► draft, shown to you │
                                    └───────────────────┬───────────────────────┘
                                                        ▼
                                        【GATE 2: you approve the finished asset】
                                                        │
 8 REPORT ◄──────────────── 7 PUBLISH ◄─── 6 SCHEDULE ◄─┘
```

A **revise** order (*"change reel 1's texture"*) re-enters the loop for that one item
only. If the item is still a plan entry, the plan changes; if it was already produced,
the draft is regenerated — and it goes back through **Gate 2**, never straight to post.

### Phase 0 — Intake & Brand Resolution
- Era One extracts: **brand key**, **task type** (weekly / campaign / one-off),
  and any **specifics** (offer details, deadline, product focus, target platforms).
- Claude loads `brands.yaml[brand_key]` + that brand's charter + memory folder.
- If the brand isn't in the registry → Claude runs the **onboarding checklist**
  (§11) instead of guessing.

### Phase 1 — Research & Listening (this is what makes it "senior")
- **Brand memory:** read the last 4–8 weeks of that brand's posts + the
  `recommendations.md` history. Learn what performed, what didn't, what's been
  posted recently (avoid repeats).
- **Charter:** load the visual + tone rules (palette, fonts, safe zones, footer,
  language, hashtag limits).
- **Trend/context pass:** for campaigns, research the offer, the season, relevant
  hooks and formats currently working in that niche. (Uses web search when
  available.)
- **Brief synthesis:** Claude writes a 4–6 line brief back to you *in Era One's
  voice*: "Here's what I'm seeing and what I propose." Fast, human, skimmable.

### Phase 2 — Strategy
- Sets the week's / campaign's **objective** (reach, engagement, conversions),
  **content mix**, and **posting map** against the brand's optimal times.
- Enforces the brand's **mandatory rules** (e.g. DC: ≥1 wardrobe reel + ≥1
  wardrobe carousel/week; reels → IG+TikTok+FB, carousels → IG+FB only).

### Phase 3 — Plan / Concept
Produces, in `working_dir/output <month>/week_<N>/` (or `/campaign_<name>/`):
- `plan.md` — objective, calendar (day, time, format, platform, hook), rationale,
  and for **every item**: the chosen material (family + exact swatch name + one-line
  why), the wardrobe reference it follows if applicable, and a `status:` line.
- `reel_<N>_script.md` — shot-by-shot (hero + clips + outro), per the charter.
- `carousel_<N>_concept.md` — slide-by-slide.
- `captions/` — per-post captions in the brand's language, with the brand footer.
- `approval_checklist.md` — exactly what you need to review.

**The plan contains no open questions.** Claude picks the materials and textures
itself, from the brand's swatch library, under the design laws. A plan that asks
*"quartz or ceramic?"* is an unfinished plan.

### ▶ GATE 1 — Plan approval, item by item
Claude presents the plan (via Era One, and by Gmail). Each item starts at
`status: pending`. You reply per item:

- approve → `status: plan_approved` — Claude produces it as a draft.
- correct → `status: needs_changes` + your note — Claude revises the plan entry.

Nothing gets produced until an item is `plan_approved`. This is where you catch
direction problems *before* wasting render time.

### Phase 4 — Production (one item at a time)
`plan_approved` items only, produced **serially** — while item N renders, item N-1 sits
as a finished draft awaiting your Gate-2 review. Never mass-generate the week in one
pass; never touch a `pending` or `needs_changes` item.

- **Design (carousels, statics, overlays):** Adobe + Python/PIL per the charter's
  exact spec (canvas size, safe zones, FINITION cards, badge placement, footer).
- **AI visuals (reels):** Magnific Space pipeline for brands that use it (DC:
  texture-swap kitchen pipeline → hero → b-roll → Kling video → Video Combiner).
- **Video assembly + music:** ffmpeg per the charter; fresh instrumental per reel
  (never reuse within a week), volume curve + fade rules from the charter.
- Everything obeys the **brand's** charter — not a generic default.

### Phase 5 — QC / Brand-Safety Check (automatic, per item)
Claude self-checks each asset against the charter and the plan, and reworks any
failure before the asset counts as done:
- Correct aspect ratio + safe zones? Logo/badge present and correctly placed?
- Palette + fonts on-brand? Footer + contact correct? Language + encoding (UTF-8)?
- **Material matches the plan?** The render shows the exact swatch the plan named —
  this is the check that catches a walnut render for a "Magnolia" plan.
- **No emoji in the description body?** (Footers keep theirs — appended verbatim.)
- **First hashtag correct?** Delta Cuisine: `#deltacuisinegroup`, always first.
- Hashtags within limit? No forbidden elements (e.g. DC: never label aluminium)?
- **Routing sanity:** does the intended destination match the brand key?

An asset that passes QC is saved as a **draft** — its plain filename, *never* with
`(approved)` — plus its `_description.txt` caption, and its plan entry moves to
`status: awaiting_review`. Claude then shows it to you (file path, material used,
caption) and emails it to the brand's notify address so you can review it away from
the machine.

### ▶ GATE 2 — Asset approval
You look at the finished asset. Then either:

- **Approve:** *"approve reel 1"* → Claude renames it to include `(approved)`, sets
  `status: approved`, and schedules/publishes it for its slot.
- **Correct:** *"change reel 1's texture"* → Claude regenerates the draft and shows
  you again. A revised asset is **not** approved just because you asked for the change.

The string `(approved)` in a filename is **your signature, never Claude's.** Claude
may only write it into a file whose finished asset you explicitly approved here.

### Phase 6 — Scheduling (routing happens here)
- Claude resolves the destination **from the registry only** — never from memory.
- Picks optimal slots from the brand's schedule (or the campaign's fixed dates).
- Queues all target platforms for the **same** timestamp so they go live together.

### Phase 7 — Publish
- Each platform posts through **whichever tool that brand's registry entry
  specifies** (`instagram_tool`/`facebook_tool`/`tiktok_tool` — currently
  Composio for both active brands' connected platforms; Zernio is legacy).
- **Host local media first.** IG/FB fetch video/images from a **public HTTPS URL**;
  a local file path (or a made-up `s3key`) is rejected, and there is no raw-upload
  tool in the headless Composio env. So for each asset, run
  `python "<Era dir>/media_host.py" "<local path>" --json`, read the `url` from its
  JSON output, and pass **that** as the `video_url` / `image_url`. (Zero-config
  default host; set Cloudinary/Bunny creds for your own CDN — see the script header.)
- **Reels/Video** → IG + FB (+ TikTok once connected), same time.
- **Carousels** → IG + FB. **Never TikTok.**
- Writes a `.posted` marker to prevent double-posting.

### Phase 8 — Report & Learn
- Gmail summary of what went out, where, and when.
- Appends results + observations to the brand's `recommendations.md` so next
  week's Phase 1 is smarter. **This is the loop that makes it act senior over time.**

---

## 4. Weekly cadence (recurring, per brand)

Each active brand can have its own two scheduled tasks in Claude Desktop:

| Routine | Suggested time | What it does |
|---------|----------------|--------------|
| **Weekly Plan + Production** | e.g. Fri 11:00 | Phases 0–3, waits at Gate 1; then produces drafts and waits at Gate 2 |
| **Auto-Publish** | e.g. Fri 21:00 | Phases 6–7 for that brand's `(approved)` files |

- **One task pair per brand**, each pointing at that brand's registry key and folder.
- Claude Desktop must be open for scheduled tasks to fire.
- If you'd rather run everything by voice on demand, you can skip the schedule and
  just say *"Era, run Delta Cuisine's weekly plan now."*

---

## 5. Campaign flow (on-demand, e.g. a promo)

Example spoken command:
> *"Era, prepare a campaign for Delta Cuisine — 'Élu Produit de l'Année 2026'
> promo, runs next week, hero reel + 2 carousels, and tell me before you build it."*

Claude runs:
1. **Phase 0–1:** resolve `delta_cuisine`, load charter, research the offer + best
   promo formats, check what's been posted recently.
2. **Phase 2–3:** propose a campaign concept — hook angle, hero reel script,
   carousel concepts, posting map across the run, VO plan (promo reels *may* use
   French voice-over per the charter; regular content never does).
3. **▶ GATE 1:** you approve the concept item by item (or name corrections).
4. **Phase 4–5:** per approved item, one at a time — produce (hero reel with VO +
   ducked music, carousels, captions), QC, save as a draft, show it to you.
5. **▶ GATE 2:** you approve each finished asset.
6. **Phase 6–7:** schedule + publish to the correct brand pages at the offer times.
7. **Phase 8:** report + log performance for next time.

A **live offer** in that brand's `offers:` block (Delta Cuisine's **-10%**) is used
without asking — it stays live until you say it stopped. Tell Era once (*"the -10% is
no longer valid"*) and it flips `active: false`, so no future content can use it.

---

## 6. Guardrails — the checks that prevent wrong-page / off-brand posts

**Brand Lock (before Phase 4 and again before Phase 7):**
Claude states, out loud via Era One and in writing, the resolved context and waits
for a yes:

```
🔒 BRAND LOCK — Delta Cuisine
   Instagram : @delta_cuisine_group   (Composio connected account — see brands.yaml)
   TikTok    : @delta.cuisine.gro      (NOT CONNECTED YET — excluded from routing)
   Facebook  : DELTA CUISINE           (Composio page 115558078454323)
   Palette/charter : MASTER_CONTENT_BLUEPRINT.md
   Notify    : kimuss92@gmail.com
   Producing : 2 reels + 1 carousel for week 28
   ►  Confirm before I build / publish?  (yes / change)
```

**Pre-publish assertion (automatic, hard stop):**
Before any `shareNow`/schedule call, Claude asserts that the destination account
IDs it's about to use **exactly equal** the ones in `brands.yaml` for the resolved
brand key. If they don't match, it aborts and asks. No exceptions.

**Every Composio call must pass an explicit page/connected-account ID from
`brands.yaml`. Never rely on Composio's default connected account.** Composio's
notion of a "default" connected account per toolkit can change (e.g. whichever
account was authorized most recently) and one connected account's token can see
pages/handles belonging to more than one brand. The `id=` value shown in the
Brand Lock is the only thing that may ever be passed as the target — never omit
it and let Composio fall back to whatever it currently treats as default.

**Charter diff (automatic):** if a produced asset violates a charter rule, it's
held at QC, reworked, and never saved as `(approved)` — so it can never be posted.

---

## 7. Publishing routing (read from the registry, per brand)

| Content Type | Instagram | TikTok | Facebook |
|--------------|-----------|--------|----------|
| Reels / Video | ✅ (brand's `instagram_tool`) | ✅ once connected (`tiktok_tool`) | ✅ (brand's `facebook_tool`) |
| Carousels | ✅ | ❌ never | ✅ |

**Current default: Composio for Instagram + Facebook, for both active brands.**
Zernio is retired for Delta Cuisine and Ceram Quartz (their free-tier accounts had
an expiry date) but stays available as an option — a future brand's registry entry
can still set `instagram_tool`/`facebook_tool`/`tiktok_tool` to `"zernio"` if that
ever makes sense. TikTok is planned for both current brands, via Composio, but
isn't connected yet — each brand's `routing` override EXCLUDES tiktok from its
reel/carousel lists until it's actually wired up, so a real publish can never try
to post through a connection that doesn't exist.

- **Each platform's tool is per-brand and per-platform** — read from
  `<platform>_tool` in that brand's block (falling back to the global default in
  `brands.yaml`'s `defaults:`). Never assume; always check the registry.
- **If a brand still uses Zernio for something:** Zernio gives 2 social slots per
  account, so plan one Zernio account per brand needing it, key in `.env` as
  `ZERNIO_API_KEY_<BRAND>`.
- **Composio** handles Gmail for all brands, plus whichever social platforms each
  brand's registry entry points at it for (currently: IG + FB for both brands).
- IG→FB crosspost is unreliable — always post FB as its own action.
- All target platforms schedule for the **same** timestamp so they go live together.
- **Timezone** is per brand (DC: `Africa/Tunis`, UTC+1).

---

## 8. Approval mechanics

Approval happens **twice**, and Era works out which gate you mean from the item's
`status` in `plan.md` — so you say the same words either way:

| You say | Item's status | What it means |
|---------|---------------|---------------|
| *"approve reel 1"* | `pending` | **Gate 1** — plan approved; Claude produces the draft |
| *"approve reel 1"* | `awaiting_review` | **Gate 2** — asset approved; Claude renames + publishes |
| *"change reel 1's texture"* | `pending` | Edit the plan entry |
| *"change reel 1's texture"* | `awaiting_review` | Regenerate the draft → back to Gate 2 |

- *"approve the plan"* with no item named clears **Gate 1** for every pending item.
- Publishing only ever touches files with `(approved)` and no `.posted` marker — the
  machine-level interlock that makes double-posting and off-plan posting impossible.
- `(approved)` is **your** signature. Claude never writes it into a filename on its
  own, no matter how urgent the order or how good the render looks.

---

## 9. Era One voice-command patterns (say these)

| You say | Era One → Claude does |
|---------|-----------------------|
| "Plan **[brand]**'s week." | Type A pipeline, stops at the plan gate |
| "Build a **[offer]** campaign for **[brand]**." | Type B, stops at the plan gate |
| "Urgent: a promo reel for **[brand]** now." | Rush lane — produces immediately, doesn't post |
| "What's scheduled for **[brand]** this week?" | Reads that brand's plan + queue |
| "Approve the plan." / "Approve reels 2 and 3, change reel 1." | Approves plan items; production starts |
| "Change reel 1's texture / description." | Revises that one item, re-schedules its slot |
| "Publish **[brand]**'s approved posts now." | Phases 6–7 for that brand |
| "How did **[brand]** do last week?" | Reads report + memory, summarizes |
| "Add a new brand: **[name]**." | Runs the onboarding checklist (§11) |

If the brand is missing/ambiguous, Era One **asks** — it never assumes.

---

## 10. Folder & file conventions (per brand)

```
<brand working_dir>/
  charter/                     — brand charter doc(s) (DC: MASTER_CONTENT_BLUEPRINT.md)
  assets/                      — logos, badges, fonts, textures, wardrobe refs
  memory/                      — recommendations.md, performance log, decisions
  output <month>/
    week_<N>/  or  campaign_<name>/
      plan.md
      reel_<N>_script.md
      reel_<N>.mp4  →  reel_<N> (approved).mp4
      reel_<N>_description.txt
      carousel_<N>_slideX.png
      carousel_<N>_description.txt
      approval_checklist.md
  trash_unused_files/          — unapproved > 7 days
```

Captions are **always UTF-8** (`encoding='utf-8'` in Python) — broken encoding
breaks whichever platforms share the affected caption/text field, regardless of
which tool (Composio or Zernio) is posting it.

---

## 11. Onboarding a new brand (do this once per brand — e.g. Ceram Quartz)

1. **Registry entry:** add a block to `brands.yaml` (copy the template, fill every
   field). Leave nothing as `TODO` before going live.
2. **Composio:** connect the brand's Instagram + Facebook (and TikTok once ready)
   in Composio; record each platform's connected-account ID / Page ID. This is
   now the default publishing path for every brand. (Zernio is legacy — only use
   it if a specific brand genuinely needs it; if so, one Zernio account per brand,
   2 slots, key in `.env` as `ZERNIO_API_KEY_<BRAND>`.)
3. **Composio / Facebook:** connect the brand's FB page, record its Page ID.
4. **Gmail notify:** pick the notification recipient for that brand.
5. **Magnific:** create/point to the brand's Space (or "N/A" if it's Adobe-only).
6. **Charter:** build the brand's charter doc — palette, fonts, logo/badge rules,
   safe zones, tone, language, footer, hashtag policy, content mix, posting times.
   (For DC this already exists as the Master Blueprint; a new brand needs its own.)
7. **Browser profile + Claude account:** assign which Brave profile + which Claude
   account this brand runs under (see the setup notes you were given).
8. **Working folder + memory:** create the folder tree above; seed `memory/` with
   any past posts so Phase 1 has history.
9. **Dry run:** run the weekly pipeline to Gate 1 only, confirm the Brand Lock shows
   the correct handles + IDs, then go live.

---

## 12. Troubleshooting

**Wrong brand / wrong page almost happened?**
→ The Brand Lock or pre-publish assertion did its job. Check the `brands.yaml`
entry for that key — a wrong ID there is the only way a mis-post gets through.

**Scheduled task didn't run?** Claude Desktop must be open. Check Scheduled Tasks.

**Didn't publish?** File needs `(approved)` in the name, no `.posted` marker, and
whatever that brand's `<platform>_tool` requires is actually connected (currently:
Composio connected for IG/FB for both brands; if a brand still uses Zernio for
something, its `ZERNIO_API_KEY_<BRAND>` must be valid in `.env`).

**No Gmail notification?** Composio Gmail must be connected; check the brand's
`notify_email`.

**Magnific failed?** Open the brand's Space link, verify inputs uploaded, confirm
the brand's generation prompt wasn't corrupted.

**Encoding looks broken (é, è, à, ç…)?** Caption file wasn't written UTF-8.

---

*This workflow is brand-agnostic. Brand specifics live in `brands.yaml` (routing)
and each brand's charter (rules). Delta Cuisine's charter = MASTER_CONTENT_BLUEPRINT.md.*
