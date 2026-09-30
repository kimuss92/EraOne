---
name: interior-design
description: Material-harmony rules (kitchen's stone/wood/plain formula, wardrobe's 3 configurations), the wardrobe conception-reference protocol, a Magnific prompt-iteration procedure, and a pre-generation checklist for Delta Cuisine kitchen and wardrobe renders. Use BEFORE writing any kitchen/wardrobe concept and BEFORE any Magnific/AI image generation for Delta Cuisine, to guarantee the design obeys the brand's material-role, cabinet-texture, tall-glass-cabinetry, wardrobe-configuration, wardrobe-accessory, and Pinterest-inspiration laws. Every wardrobe concept must reproduce the global conception (module layout, proportions, door division) of the reference images in the brand's "modular wardrobe references" folder, varying only texture, ambiance and setting, and must pick materials by exact name from the brand's texture library. The authoritative machine-readable copy lives in brands.yaml under delta_cuisine.design_rules; this skill is the reasoning procedure that applies it.
---

# Interior Design — Delta Cuisine material & harmony gate

The single source of truth for the numbers/allowed values is
`brands.yaml → brands.delta_cuisine.design_rules`. This skill is the **procedure**
that turns those rules into a hard pre-generation checklist. If this skill and
`brands.yaml` ever disagree, **`brands.yaml` wins** — re-read it.

## When to run this
- BEFORE writing or revising a kitchen/wardrobe **concept** document.
- BEFORE any Magnific/AI **generation** of a kitchen or wardrobe image.
- When reviewing a render that already exists (validate it against the checklist).

## The rules (Delta Cuisine)

### Kitchen — the three-material formula
UPDATED 2026-07-20: every surface has an assigned role. Exactly one swatch per
role, three roles total:

1. **STONE** — ONE quartz OR ceramic texture. Used ONLY on the island
   countertop, its waterfall edges, and the backsplash. Nowhere else.
2. **WOOD** — ONE wood-grain finish. ALL lower cabinetry facades, matched
   exactly across every lower unit.
3. **PLAIN** — ONE solid lacquer colour. ALL upper cabinetry facades.

Fixed brand furniture, **not** part of the three-material count:
- **Tall cabinetry** — always dark glass.
- **Plinth** — kitchen base is always black matte aluminium.
- **Appliances** — always Whirlpool or Rosières branded, never generic.

**Harmony:** the stone/wood/plain combination must read as harmonious — no
clashing finishes. **Never:** a fourth material, a second stone texture,
mismatched lower-cabinet facades, or an unbranded appliance.

### Wardrobe
Pick exactly ONE configuration per wardrobe — three options, corrected
2026-07-20 (the prior version of this list had a duplicate pair and was
missing the third real option):
- **CONFIG_A** — one texture only, lacquered or laminated, no glass; **or**
- **CONFIG_B** — all glass doors with a laminated-wood interior; **or**
- **CONFIG_C** — combination: one lacquered/laminated texture PLUS glass-door
  modules with a laminated-wood interior.

**Full generation-ready prompt system** (template, per-config variable table,
constraints, negative prompt) lives in the vault:
`Resources/Delta_Cuisine_Wardrobe_Master_Prompt.md`. This skill governs *which*
config and materials are correct; that note governs *how to phrase the
Magnific prompt* once the choice is made.

**Wardrobe generation goes into its OWN Magnific space** — `a24e4d72-2ef9-4d1a-9e55-2de12fad843a`
("Modular wardrobe claude" — a real node-graph workflow, rebuilt 2026-07-20;
see `.claude/skills/magnific-space-expert/SKILL.md` for how to run/edit it),
never the brand's kitchen space (`a243e483-a215-4f17-895c-0896895bc3f3`) and
never the old superseded "DC wardrobe space". Confirm which content type
before picking the space; mixing them puts the render in the wrong Magnific
project.

### Wardrobe accessories — MANDATORY, capped (added here 2026-07-20)
Every wardrobe visual shows interior fittings from `Wardrobe premium
accessories/` — **required**, not optional, but **1–3 accessories max**, never
the full set. State exact placement per accessory (e.g. "shoe rack in the
lower-left module"), tied to the module layout of the reference the wardrobe
follows. Vary the combination week to week. The accessory must be **visible**
— through glass (CONFIG_B/C) or with a module shown open (CONFIG_A).

### Wardrobe conception reference (HARD — do not skip)
Past wardrobe concepts **drifted away from the real Delta Cuisine product line**:
they invented wardrobe forms we do not build. To stop that:

1. **Open the reference images before concepting.** They live in
   `<project_dir>/modular wardrobe references/` — six files, `modular wardrobe
   ref.png` through `ref6.png`. The CM runs with `cwd = project_dir`, so that
   relative path resolves directly. Actually look at them; do not work from memory.
2. **Reproduce their global conception.** Module layout, proportions, door
   division and overall structure must correspond to one of the six references.
3. **Only these may vary:** texture, colour, ambiance, lighting, scene/setting.
4. **Name the reference** the concept follows, explicitly, in the concept document.
5. A wardrobe whose layout matches none of the references is **wrong at the
   source** — rework the concept, not the prompt. Same gate as the material laws.

### Materials come from the texture library, never invented
Pick every material from the brand's swatch library at
`brands.yaml → delta_cuisine.texture_folder`
(`<project_dir>/textures/`), which is organised as:
- `bois/` → `brillant/`, `matt/`, `stratifié/` (e.g. `CACHEMIRE MAT.jpg`)
- `Ceramique/` (e.g. `Negro Marquina Matt.jpg`, `Calacatta Imperial.webp`)
- `Quartz/` (e.g. `Nero Stellar.jpg`, `ABSOLUTE BLACK.jpg`)

Use the **exact swatch name** from that library. Never invent a material name and
never substitute a lookalike. Choosing *which* swatch is yours to decide; inventing
one that isn't a real file in the library is the only forbidden move.

### Inspiration
- Get inspired from **Pinterest** references before concepting. Do not invent
  layouts blind — anchor to a real reference direction first.
- ⚠️ For **wardrobes**, Pinterest is for *mood and ambiance only*. The **form** is
  fixed by the conception references above — Pinterest never overrides them.

## Pre-generation checklist (must PASS before generating)

Answer every item explicitly in the concept before generating. **You decide every
answer yourself — never ask the user.** Materials, textures and finishes are the
CM's professional call: pick from the swatch library, state the pick and a one-line
reason in the concept, and generate. An "unspecified" answer is not a question to
send back; it is a decision you still owe. The user steers by correcting a named
item at plan review ("change reel 1's texture"), not by answering questions mid-run.

How to choose well, in order:
1. **Read the brief and the charter** — the concept's mood, the slot (design-tip vs
   avant/après vs promo), the season.
2. **Check what ran recently** — the vault run log (`Era_Run_Log_delta_cuisine`
   in the Obsidian vault's Areas folder), past `<base>_description.txt`
   captions in the project folder, or the brand's live Instagram feed
   (`WebFetch` the public profile — see the CM's global "CM Operating
   Principles" vault note, `era_brain/Areas/CM_Operating_Principles.md`, for
   when to do this). Do not repeat a swatch used in the same week; vary family
   across the week. *(There is no `memory_dir` folder — that path in
   brands.yaml was dead config, corrected 2026-07-20; use the sources above instead.)*
3. **Open the swatch library** and pick the exact file whose colour/finish serves
   the concept's mood and passes the harmony law below.
4. **State it:** *"Quartz — Nero Stellar (matt) for the STONE role, chosen for
   the high-contrast avant/après reveal against the CACHEMIRE MAT (WOOD role)
   lower cabinetry."*

- [ ] **All three kitchen roles named?** STONE (exact slab), WOOD (exact
      lower-cabinet grain), PLAIN (exact upper-cabinet lacquer colour) — each
      pinned to a real swatch file, none left generic.
      → *This is the check that would have caught the walnut-instead-of-Magnolia render.*
- [ ] **STONE used only where it belongs** — countertop + waterfall + backsplash,
      nowhere else (not on cabinetry)?
- [ ] **No fourth material** sneaking in beyond stone/wood/plain?
- [ ] **Tall cabinetry** stated as dark glass, plinth as black matte aluminium,
      appliances as Whirlpool/Rosières?
- [ ] **Wardrobe** (if any) matches exactly one of the 3 allowed configurations
      (CONFIG_A/B/C — full prompt spec in the vault's
      `Resources/Delta_Cuisine_Wardrobe_Master_Prompt.md`)?
- [ ] **Wardrobe conception reference opened and named?** The concept states which
      of the six `modular wardrobe references/` images it follows, and its module
      layout / proportions / door division reproduce that reference.
      → *This is the check that catches wardrobes drifting off the product line.*
- [ ] **Wardrobe variation stays inside bounds?** Only texture, colour, ambiance,
      lighting and setting differ from the reference — never the form.
- [ ] **Wardrobe accessories present and capped?** 1–3 from `Wardrobe premium
      accessories/`, each with a stated module position, actually visible in the
      shot. Absent entirely is now a fail (mandatory since 2026-07-20); more than
      3 is also a fail.
- [ ] **Correct Magnific space?** Kitchen → kitchen space; wardrobe → the
      dedicated wardrobe space. Never mixed.
- [ ] **Every material named from the texture library?** Exact swatch name from
      `textures/{bois,Ceramique,Quartz}` — nothing invented, nothing approximated.
- [ ] **Pinterest inspiration** referenced for the layout/mood?
      (Wardrobes: mood only — form comes from the conception references.)
- [ ] **Harmony** self-check: do the chosen surfaces read as one coherent palette?

## If a generated render fails the checklist — revise the prompt, don't just re-roll

A failed check is a prompt problem, not a bad-luck problem. Fix the specific
clause that caused it (e.g. the stone texture leaked onto cabinetry → state
"STONE appears ONLY on countertop/waterfall/backsplash" more forcefully in the
prompt, don't just regenerate the same prompt hoping for a different result).
Up to 2 retries with a revised prompt before showing the asset to Karim; if it
still fails on the 3rd attempt, show it anyway and state plainly which check it
fails rather than silently shipping a render that doesn't pass.

## How this plugs into a produce run
`era_dispatch.build_brief` injects the `design_rules` for Delta Cuisine directly
into every produce brief, so the headless job sees them even without loading this
skill. This skill is the human-and-agent-readable expansion: run its checklist,
put the answers in the concept, and only then generate. A render that fails any
material item is **wrong at the source** — fix the concept, not just the prompt.

A headless run **cannot receive an answer to a question** — asking one just ends the
run with nothing produced. That is exactly the failure this skill exists to prevent:
decide, state, generate, and let the plan review catch anything the user wants
different.
