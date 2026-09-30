---
name: magnific-space-expert
description: How to read, run, and safely edit a Magnific Space (the node-based visual generation workflow — not just a project folder) like an expert, without breaking the hard rules baked into it. Use BEFORE running a generation in any production Space (e.g. Delta Cuisine's "Modular wardrobe claude"), BEFORE editing a Space's node graph, and when evaluating whether a technique from a shared/community Space is worth adopting. Covers the full spaces_* tool surface, the vocabulary (nodes/connections/panels/ports), a safe-edit discipline that protects rule-carrying nodes, a self-improvement loop for tuning a Space from real generation outcomes, and how to learn from shared templates without importing content that doesn't belong.
---

# Magnific Space Expert

A Magnific **Space** is a visual node-graph workflow — nodes (creation inputs,
Claude-powered prompt-generators, image/video generators, list collectors,
combiners, panels) wired together with typed connections (image/text/video
ports). It is NOT a folder. Treat it as a small program: nodes are steps,
connections are data flow, and the prompt-generator nodes' `instructions`
fields are where a brand's hard rules live in executable form — not just in a
document somewhere the CM has to remember to apply.

Worked example throughout: Delta Cuisine's **"Modular wardrobe claude"** space
(built 2026-07-20, see `brands.yaml → delta_cuisine.design_rules.wardrobe`
for its id). Read that space's actual node graph as you read this skill —
it's the concrete referent for every abstract point below.

## The tool surface

| Tool | Use for |
|---|---|
| `spaces_list` | Find a space by name; `ownership="shared"` surveys shared/community spaces |
| `spaces_state` | Read the full node graph. `scope="current_page"` (default) is cheap; `scope="all"` reads every page — costly, use sparingly |
| `spaces_get_nodes` | Cheaper than `spaces_state` when you only need specific node ids + their connections |
| `spaces_create` | New empty space |
| `spaces_edit` | The ONLY way to modify a space — natural-language `query`, executed by a specialist agent on Magnific's side. Async: poll `spaces_edit_status` |
| `spaces_edit_status` | Poll with `operationId` + `runId` until `allTerminal: true`, THEN re-read with `spaces_state` to verify — the status call confirms the edit finished, not that it did what you asked |
| `spaces_add_creations` | Add existing creations as new image nodes (doesn't replace an existing node's content) |
| `spaces_run` | Execute the workflow from `startNodeId`. `mode="downstream"` (default) runs that node onward without rerunning upstream — the one to use for a normal generation. `mode="connected"` reruns the WHOLE connected graph including already-succeeded nodes — destructive, only for an explicit full rebuild |
| `spaces_run_status` | Poll a run; on completion, use `creations_show`/`creations_wait` for the actual asset |

## Vocabulary

- **Node types you'll see**: `creation` (a fixed input image/reference), `text`
  (an editable plain-text note), `prompt-generator` (a Claude/GPT node that
  reads attachments + instructions and writes a `generated_prompt`),
  `image-generator`, `video-generator`, `video-combiner`, `list` (collects
  multiple outputs — `mode: accumulate` keeps adding, `mode: replace` swaps
  each run), `panel` (a labeled, colored grouping box — visual only, not a
  data node).
- **Connections** are typed (`image`/`text`/`video`) and directional
  (`sourceElementId.sourcePort → targetElementId.targetPort`). A
  prompt-generator's real output port is `generated_prompt`; an
  image/video-generator's is `output`; a list's are `output-images` /
  `output-texts` / `output-videos` depending what it collects.
- **`@[nodeId:nodeName:port]`** inside an `instructions` field is a live
  reference to another node's content — this is how a prompt-generator "sees"
  an attached image or reads another node's text without it being a wired
  connection port. Both the visible connection AND the `@[...]` mention
  usually exist together; removing one without the other silently breaks
  the reference.

## Reading a space before touching it

Always `spaces_state(scope="current_page")` (or `spaces_get_nodes` for a
narrower read) before running or editing. Never assume you remember a space's
current wiring from a past session — an edit (yours or the user's, in the
Magnific UI) may have changed node ids, and hardcoding a UUID from memory is
exactly how a run silently targets the wrong node.

## Running a generation in a production space

1. Read current state, find nodes **by name**, not by a remembered UUID
   (names are stable across edits; ids sometimes aren't).
2. Update the input nodes for this run via `spaces_edit` — e.g. "replace the
   image in the 'Texture / Finish' node with creation identifier X" or
   "update the 'Config + Materials' text note to: CONFIG_C — laminate Rovere
   Naturale + glass doors; shoe rack in lower-left module." Poll
   `spaces_edit_status` to completion.
3. `spaces_run(startNodeId=<the first real generator node after your inputs>,
   mode="downstream")` — this regenerates that node and everything wired
   after it, without re-touching upstream input nodes you just set.
4. Poll `spaces_run_status`; on completion, `creations_show`/`creations_wait`
   for the actual output.
5. Self-check the result against the brand's design-rules checklist (e.g. the
   `interior-design` skill for Delta Cuisine) BEFORE showing it — a space
   that's wired correctly can still produce a bad generation; the graph
   enforces structure, not outcome quality.

## Safe editing — preserve the rules

**Before any `spaces_edit` call, identify which nodes carry hard rules.** In
"Modular wardrobe claude": the Hero Prompt Architect's `instructions` field
*is* the conception-reference anchoring rule, the 2-material cap, the
mandatory 1–3 accessories, and the negative prompt — all in one place,
executable. Editing that node's instructions IS editing the brand's design
rules, not just tuning a prompt. Treat it with the same care as editing
`brands.yaml → design_rules` directly, because functionally that's what it is.

**Rule of thumb**: an edit is safe when it changes *how well* the rules are
expressed (clearer wording, a camera-movement library, panel layout, adding a
comparison branch) and unsafe when it *removes or loosens* a constraint
(dropping the negative prompt, making accessories optional, allowing a 4th
material, silently reverting the cost-tiered resolution split).

**After every edit that touches a rule-carrying node**, re-read it with
`spaces_get_nodes` and check the rule text survived verbatim or equivalent —
don't trust the edit summary alone. If a rule clause is gone or weakened,
edit again to restore it before running anything.

**Never let a "make it better" edit silently swap the resolution/model
tiering.** The hero/supporting-shots split (2K for the hero, 1K for
supporting shots — see `Production_Standards_AI_Generation.md`) is a
credit-cost decision. An edit that "improves quality" by bumping everything
to 2K is a real cost regression dressed up as an improvement; catch this the
same way you'd catch a dropped design rule.

## QC and selective re-render — deliver a finished result, not a first draft

**The CM is expected to hand over a finished, correct Reel — not whatever the
first pass produced.** A space can be wired perfectly and still generate an
individual shot that drifts (wrong contents in a module, a material that
doesn't match the hero, a duplicate-looking angle, a camera move that reveals
something it shouldn't). Catching and fixing that is part of the job, not an
optional extra.

### The QC pass — after images, before video

Once the stills exist, **actually look at them** (`creations_wait` for the
URL, download, and view — not just "the node says completed"). Check each
supporting shot against the hero and against the brand rules:

- Same finish, materials, module layout and door division as the hero?
- Accessory in the same module position as the hero states?
- Module contents correct? (For Delta Cuisine: upper cabinets hold ONLY
  bedding/linen — pillows, bedspreads, duvets — never hanging clothes;
  clothes only in the main hanging section; shoes only in lower modules.)
- Is this shot genuinely a DIFFERENT type from every other one, or does it
  read as a near-duplicate? Complementary B-roll, not variations of one photo.
- Negative-prompt violations: people, text/watermark, clutter, a third
  material, warped lines, tilted verticals?

### Re-rendering ONE shot without redoing everything

This is the key mechanic. `spaces_run` takes a `startNodeId` and a `mode`:

```
# Re-roll just shot 3's image (same prompt, new generation):
spaces_run(spaceId, startNodeId=<Shot 3 Image (1K)>, mode="singular")

# Re-write shot 3's PROMPT and regenerate its image
# (when the shot is wrong at the source, not just unlucky):
spaces_run(spaceId, startNodeId=<Shot 3 — Macro Material>, mode="downstream")
```

`mode="singular"` runs only that node. `mode="downstream"` runs it and
everything after it. **Never use `mode="connected"`** for a fix — it reruns
the whole graph including already-good nodes, wasting credits and re-rolling
shots you were happy with.

**If a shot is wrong twice for the same reason, the prompt is wrong, not the
generation** — edit that shot's prompt-generator instructions to close the
specific gap (per the safe-editing rules above), then re-run from the
prompt node. Don't re-roll a third time hoping for luck.

### Rebuilding the video after a still is replaced

The collector list and everything downstream still hold the OLD clip. After
replacing any still, re-run the video stage so the Reel picks it up:

```
spaces_run(spaceId, startNodeId=<All Stills>, mode="downstream")
```

That re-collects the stills, re-writes camera prompts, regenerates the clips
and re-combines. Same QC discipline applies to the finished video: download
it, check duration matches (clips × seconds), pull frames across its length,
and confirm the shots are actually varied and consistent before showing it.

### When to stop

Two re-rolls per shot. If a shot still fails on the third attempt, stop
re-rolling — show the user the result and say plainly which check it fails
and why, rather than burning credits or quietly shipping something that
doesn't meet the rules.

## Self-improvement — tune the space from real outcomes, and log why

A space is not "done" once built. When a generation fails a QC check (wrong
config leaked through, camera motion revealed a material that shouldn't be
visible, an accessory missing or a catalogue of them):

1. **Don't just regenerate blindly.** Identify which node's instructions
   caused the failure and tighten the specific failing clause via
   `spaces_edit` — e.g. if CONFIG_A renders leaked glass, strengthen the Hero
   Prompt Architect's CONFIG_A branch to state "absolutely no glass anywhere,
   not even a single door" instead of hoping a re-roll avoids it.
2. **Log the change and why** in the vault — append to a "Magnific Space
   Changelog" note (`Areas/Era_Space_Changelog_<brand>.md`, same pattern as
   `Era_Run_Log_<brand>.md`: create if missing, append under a fixed heading,
   never edit history). One line: date, what failed, what was changed, node
   name. This is what makes tuning cumulative instead of the same mistake
   getting silently re-fixed (or re-missed) every few weeks.
3. **A tuning edit is still an edit** — it goes through the same safe-editing
   discipline above. Verify the rule-carrying content survived, and verify
   the specific new clause is actually present after the edit completes.

## Learning from shared templates

`spaces_list(ownership="shared")` surfaces spaces others have shared with
this account — real, working community/template workflows. Worth surveying
periodically, and worth reading whenever this skill or a brand's space feels
like it's missing a capability another workflow clearly has.

**What transfers: structural technique, never content.** A shared UGC-ad
space's actual prompts, subjects, and brand voice are irrelevant to a Delta
Cuisine wardrobe render — importing them would be exactly the kind of
cross-brand-style contamination this whole project is built to prevent.
What's worth adopting is the *pattern*:

- **Fan-out to multiple models from one prompt, for comparison** — feeding
  the same `generated_prompt` into several video-generator nodes on
  different models/tiers so a human picks the best result. Worth adding to a
  production space when quality is genuinely uncertain and worth comparing,
  not as a standing default (it multiplies credit spend).
- **Reference-image compositing before generation** — two separate reference
  images feeding a prompt-generator that composes them into one new prompt,
  whose output becomes the shared "first-frame"/reference for everything
  downstream. This is exactly the pattern "Modular wardrobe claude" already
  uses (texture + conception-reference → hero prompt → hero image feeds every
  later step) — recognize it as the same idiom when you see it elsewhere.
- **Cost-tiered model choices within one pipeline** — a cheap/fast model for
  an intermediate or exploratory step, the fuller/costlier model reserved for
  the shot that actually needs it. Confirms the hero-2K/supporting-1K split
  is standard practice on this platform, not an arbitrary constraint.
- **Grounding a prompt-generator in external content** (e.g. reading a
  product page) — relevant if a future space needs to pull from a brand's
  own charter/report text rather than only static instructions.

When adapting a pattern into a brand's space, do it via the same safe-editing
discipline above: describe the STRUCTURAL change in the `spaces_edit` query
(new comparison branch, new compositing step, a tiering change), never paste
in another space's actual prompt text.

## Related
- `brands.yaml → delta_cuisine.design_rules.wardrobe` — the rules this space
  encodes; authoritative if the space and this file ever disagree.
- Vault `Resources/Delta_Cuisine_Wardrobe_Master_Prompt.md` — the same rules
  in prose (merged 2026-07-22 from two notes into one), for reference and for
  onboarding a new space or a new brand.
- Vault `Resources/Delta_Cuisine_Wardrobe_Accessories_Catalogue.md` — the 14
  real accessory products, their mechanisms, and correct ergonomic placement.
- `.claude/skills/interior-design/SKILL.md` — the pre-generation/QC checklist
  a space's output still has to pass, structure aside.
