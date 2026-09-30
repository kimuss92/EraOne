---
name: magnific-ad-creator
description: How to build ad creative for Delta Cuisine and Ceram Quartz with Magnific's MCP surface — flow selection, the design_auto_resize format fan-out, the mandatory free cost preflight, and the two capabilities that LOOK available but are not (Agents, Brand Kit). Use in the `adcreative` dispatch lane, and BEFORE running any Magnific flow or passing brand parameters to images_generate. brands.yaml's `<brand>.magnific` block is authoritative on flow ids, ratios and the credit ceiling; if this skill and brands.yaml disagree, brands.yaml wins.
---

# Magnific Ad Creator — ad creative for Delta Cuisine & Ceram Quartz

Single source of truth for flow ids, ad ratios and the credit ceiling:
`brands.yaml → brands.<key>.magnific`. This skill is the reasoning procedure
that turns those into good ad creative. It sits **under** the brand's hard
design rules (`brands.yaml → design_rules`, plus `interior-design` for Delta
Cuisine) and **beside** the creative bar (`reel-art-director`). It replaces
neither.

## What Magnific actually gives you (verified live 2026-07-29)

Two heavily-marketed features do **not** exist at the MCP layer. Both fail
*silently*, which is why they get top billing here.

### Magnific Agent — not callable

`agent` is a library object *type*, not a tool. `library_list(type="agent")`
returns your saved agents; there is **no `agent_create` and no `agent_run`**,
and `library_create` / `library_share` don't even accept the type. This
account owns **zero** agents. Creating and running one is web-UI only.

The only agent-shaped things you can actually invoke:

| Tool | What it is |
|---|---|
| `spaces_edit` | Natural-language mutation of a Space's node graph. Async — poll `spaces_edit_status`, then **re-verify with `spaces_state`** (status confirms completion, not correctness). Governed by `magnific-space-expert`; read it before editing any production Space. |
| `video_plan` | Server-side planning agent. Read-only, free. Call it first for any video request. |

### Brand Kit — advertised, unreachable

`images_generate` accepts `brandKitId`, `templateSlug`, `validationRunId` and
`validationScenarioKey`, described as applying "brand palette, logo, typography
and reference imagery automatically." **The tools that issue those ids —
`brand_kit_list` and `brand_kit_get` — are not exposed.** There is no way to
obtain a valid value.

**Never pass any of those four parameters.** An invented id does not raise an
error; it changes the output. Brand compliance comes from the DESIGN RULES
block in the brief and the brand charter — nowhere else. Magnific is a
generation engine, not a brand authority.

> If `brand_kit_get` ever appears in the tool list, that changes the design:
> a real Brand Kit per brand becomes the better enforcement point. Re-check on
> any Magnific MCP update and update `brands.yaml`'s comment block.

## Cost — read this before every generation

The account is Premium+ with unlimited mode, but **`unlimitedAppliesHere` is
`false` in MCP sessions**: every generation bills a finite balance (6,927
credits as of 2026-07-29). The headless run never sees a bill, so cost
discipline has to be deliberate.

1. **Preflight, always.** `simulate_flows` and `simulate_cost` are free and
   read-only. Call one before every `flows_run` / `images_generate` /
   `video_generate`.
2. **Respect the ceiling.** `magnific.credit_ceiling_per_run` (600). Over it:
   report what it would have cost and what you'd do instead, then **stop**.
   Never spend past the ceiling to "finish the job."
3. **Reuse beats generating.** Check the brand's folder for an approved asset
   that fits before generating anything. Reuse costs 0.
4. **Fan out, don't re-generate.** One approved visual →
   `design_auto_resize(aspectRatios=[...])` gives every placement in one cheap
   call, with consistent framing. Generating each size separately costs N times
   as much and drifts between sizes.
5. **Never touch `banned_flows`.** The UGC flows (10,621) exceed the entire
   balance; the video flows (3,360–4,200) need an explicit spoken order.

## The procedure

```
flows_get(id)            # read the real input spec — never guess inputs
  → simulate_flows(...)  # FREE. compare against the ceiling
  → flows_run(...)       # returns workflowRunIdentifier
  → flows_wait(...)
  → creations_show(...)  # actually LOOK at it
  → design_auto_resize(creationIdentifier, aspectRatios=<brand ad_ratios>)
```

Chain by creation `identifier`, never by `webUrl`. Use `design_auto_layers`
only when the copy genuinely has to sit in editable layers — note there is no
`design_edit_text` or `design_export` in MCP, so anything after
`design_auto_*` is web-UI work.

### Choosing a flow

Pick from the brand's `approved_flows` only. Rough map:

- **Delta Cuisine** — `otqh829FPM` (lifestyle interior scene) to place a render
  in a styled room; `sG1l8e2GTt` (multi-angle) for four angles of one kitchen;
  `wVZ7EIQVNR` (render→photoreal) when a result reads as CGI.
- **Ceram Quartz** — `CQXtEEyuoQ` (change material/colour) to swap a slab finish
  in an existing in-situ shot; `LYSswO4tgm` (colour variants) for one kitchen in
  several slabs; `eqpdqL9Ptk` (localize) FR→AR, design untouched — this brand
  posts Arabic descriptions and already has `caption_footer_ar`.
- **Both** — `X5DBfolnSW` (multi-format resizer) and `DKTpclpxKi`
  (audience-driven ads, one claim per audience).

## Hard limits

**Flows cannot hold identity across shots.** They are single-shot
input→output. A wardrobe Reel generated as six flow runs produces six
*different* wardrobes — the exact drift the master-anchored pipeline exists to
prevent. Wardrobe Reels and any multi-shot set that must keep one product
identical stay on `wardrobe-reel-workflow`: generate ONE master, DERIVE every
other shot from it. Flows are for **still ad creative and format fan-out**.

**A flow output is a photo, not a slide.** For Ceram Quartz, carousel slides
are built by placing that photo into `offre.psd` in real Photoshop via the
`photoshop` MCP (charter §6). A flow cannot reproduce the logo lockup, name-bar
or footer band — never let it render brand text, logo or footer.

**Magnific writes no text.** Every caption, hook, headline and on-image line is
yours, from the caption rules, charter, vault notes and lessons. Never ship a
generated image carrying invented brand copy — and never invent a price or a
product claim (Ceram Quartz's six approved claims are in
`design_rules.approved_claims`).

## Where the output goes

The `adcreative` lane stops at a **draft**. It does not publish, and it does
not touch an ad account — that's the separate `ads` lane, which needs the
budget decision first and is deliberately unable to generate.

Per variant, in the brand's `project_dir`:

- `<base>.png` / `<base>.mp4` — plain filename, **no `"(approved)"`**. That
  string is the user's signature; Era never writes it.
- `<base>_description.txt` — the caption, which is what the `publish` lane
  reads.
- Push each one to the user:
  `python era_telegram.py asset "<abs path>" --caption "<name> — awaiting your approval"`

From there the existing `approve` → `publish` lanes take over unchanged. Obeying
this filename contract is the entire integration — there is no separate
publishing path for ad creative.

## Before you show anything

Apply `reel-art-director`'s self-critique bar, and download and actually view
the result — never trust a "completed" status. Then state, in your summary,
**the credits this run actually spent**. A run that quietly burned the ceiling
is a failed run even if the picture is good.
