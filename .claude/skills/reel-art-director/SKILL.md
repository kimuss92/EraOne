---
name: reel-art-director
description: Creative-direction playbook for producing stunning, professional-grade product Reels and images (Delta Cuisine kitchens & wardrobes, Ceram Quartz slabs) — the art-direction layer ABOVE the mechanical Magnific-Space wiring. Use BEFORE generating any Reel or hero image set, and whenever a result is technically correct but looks flat, empty, amateur, or fails to sell the product. Covers what makes a render read as premium (styling, realism cues, human warmth, lighting), the shot grammar for a scroll-stopping vertical Reel (shot types, wide↔close montage rhythm, the two motion styles), music matching, and a self-critique bar to hold every result to before showing the user. It does NOT replace the brand's hard design rules (brands.yaml / interior-design) or the space mechanics (magnific-space-expert) — it makes the output within those rules actually look professional.
---

# Reel Art Director

The brand rules (`brands.yaml`, `interior-design`) say what is *correct*. The
space skill (`magnific-space-expert`) says how to *run the machine*. This skill
is the third thing: how to make the output actually look like a professional
studio made it, not a compliant-but-flat AI render. A shot can pass every hard
rule and still be boring. This is the bar that catches boring.

## The premium bar — what separates a pro render from an AI render

A render reads as amateur when it is **empty, sterile, or obviously synthetic**.
Fix each, every time:

- **Never empty.** A wardrobe with bare rails, a kitchen with clear countertops,
  a room with no life — reads as a showroom stub, not a home someone lives in.
  Fill it truthfully: a real couple's clothes (men's AND women's), real
  premium accessories, a folded throw, a coffee cup and a book on the island, a
  styled tray, fresh greenery. Enough to feel lived-in; never so much it becomes
  clutter (the negative prompt still forbids clutter).
- **Human warmth without people.** The brand rule bans people in frame, but the
  space must feel *inhabited* — an open book, a draped jacket over a chair, soft
  daylight through a sheer curtain, a warm lamp just out of the main subject.
  Implied presence sells aspiration.
- **Real-world light.** Soft directional daylight from one clear source plus a
  warm accent (the integrated LED strips). Gentle falloff, believable soft
  shadows, no flat even CGI lighting, no double shadows, no blown highlights.
- **Material truth.** The finish's real grain, sheen and micro-reflections must
  read — matte reads matte, lacquer catches a soft gradient, stone shows its
  veining. This is the whole point of a furniture brand's content.
- **Composition discipline.** Perfectly straight verticals, considered framing,
  breathing room. A slightly-off vertical or a cramped crop instantly reads
  amateur.

## Shot grammar for a scroll-stopping vertical Reel

A great product Reel is an EDIT, not a slideshow of one subject. Build it from
**complementary B-roll** — every clip earns its place by showing something the
others don't, and they cut together as one piece.

**Cover a range of shot types (never repeat one):**
- Hero / commercial-clean establishing — the full product, straight and polished.
- Medium three-quarter — the product in its room, natural angle.
- Close-up — a door line, a handle, a countertop edge, a faucet.
- Macro — material grain, finish sheen, craftsmanship detail (tiny travel).
- Low angle (down-top) — emphasises height and presence.
- Top-down / high angle — into an open drawer or module, showing organisation.
- Artistic — moody editorial light, a bolder composition, the "wow" frame.

**A different SUBJECT, not just a different label.** Naming two shots "close-up"
and "macro" does not make them different if both land on the same door edge and
handle — that is one shot rendered twice, and it is the most common way a Reel
ends up looking repetitive. Lock each slot to its own subject *and* its own
camera position. Before generating, read the slot list and ask: could any two of
these come back looking like the same photograph? If yes, re-assign one.

**The wide↔close montage rhythm.** Alternate scale so the Reel breathes: a wide
or medium shot, then a tight close/macro, then wide again. Two wides back to
back feels slow; two tight shots back to back feels claustrophobic. The macro
accents are the breathing points between the bigger moves. Write the running
order out as W/C/W/C/W/C and check it before you generate, not after.

**Fluid, continuous motion.** Each clip should hand off to the next on
compatible motion energy so the whole thing reads as one smooth piece. Camera
moves must never reveal something the still didn't have (a different material, a
missing accessory) and never fight the lighting direction.

**Transitions — the cut is a shot too.** Six good clips laid end to end still
read as six videos unless the cuts are designed:
- **Motion handoff.** Every clip ends on movement the next clip continues —
  same screen direction, compatible energy. A clip that ends static followed by
  one that starts static is the jarring cut people feel but can't name.
- **Match cut on a graphic element.** Land the eye in the same place across the
  cut: a vertical door line, the handle axis, the LED strip. The strongest cuts
  in furniture film are on a continuing vertical.
- **Never reverse screen direction** between adjacent clips, and never jump-cut
  two framings of the same subject.

**Reference lock — zero drift.** Every clip is the SAME physical product in the
SAME room under the SAME light. The hero image is the master; supporting shots
may vary camera position, framing, depth of field and which door is open —
nothing else. A shot that quietly relights the scene, adds a module, changes a
handle or swaps a finish is a rejected shot, not a creative variation. Drift is
the fastest way a Reel stops looking like a real catalogue shoot.

**Show the door.** When a module is open, its door is in frame — on its hinge or
slid aside, edge thickness and inner face visible. An interior with no door
attached is a shelving unit, not the product. (Wardrobes: hard rule in
`brands.yaml → facades.door_visible_when_open`.)

## The two motion styles (pick ONE per Reel; camera + music must match)

Stated per Reel; the same choice drives BOTH the camera prompts and the music.

- **Style 1 — cinematic slow motion.** Slow, fluid push-in/push-out and gentle
  glides; long unhurried takes; elegant. **Music:** a cinematic score with a
  soft underlying beat. Best for a premium, aspirational, calm mood.
- **Style 2 — modern montage.** Speed ramps, snappier multi-shot cutting, modern
  commercial energy. **Music:** electronic beats, montage-driven. Best for a
  punchy, trend-forward, high-energy mood.

Never mix them — slow cinematic camera with an electronic montage track (or
vice versa) reads as a mistake. When the music is generated (`music_generate.py`),
its prompt must name the same style's mood, tempo and instrumentation, and its
duration must match the assembled video length.

## Self-critique — hold every result to this before showing the user

Look at the actual generated stills and the actual combined video (download and
view — don't trust "completed"). For EACH, ask:

1. Does it obey every brand hard rule? (materials, module/facade rules, bedding
   rule, faucet rule for kitchens — check the brand's `interior-design` list.)
2. Does it look *lived-in and premium*, or empty/sterile? If empty, it fails —
   re-prompt with real contents.
3. Is every shot a genuinely different type, or do two read the same? If two are
   near-duplicates, re-roll one as a different type.
4. Is the lighting believable, verticals straight, no CGI tells?
5. Does the Reel's rhythm alternate wide↔close and flow as one piece?
6. Would a real furniture brand actually post this? If you hesitate, it's not
   ready.

Anything that fails 1–6 gets a **selective re-render** (see
`magnific-space-expert`: `spaces_run` a single node with `mode="singular"`, or
re-prompt then `mode="downstream"`), not a shrug. Two re-rolls per shot; if it
still fails, show the user and say plainly what's wrong — never quietly ship a
flat result. **The deliverable is a finished, post-ready Reel, not a first
draft.**

## Ideas & trends — bring something, don't just execute

A pro art director proposes. Pull a current trend or a strong reference
(Pinterest, or a shared Magnific template's structural technique) and adapt it
to the brand with the brand's own materials — never copy another brand's actual
content. A fresh angle, a bolder hero composition, a new styling idea: propose
it as a plan item, through the normal Gate-1 approval, not as a silent change.

## Related
- `brands.yaml → delta_cuisine.design_rules` — the hard rules this works within.
- `.claude/skills/interior-design/SKILL.md` — the per-render QC checklist.
- `.claude/skills/magnific-space-expert/SKILL.md` — running/editing the space,
  the selective re-render mechanics this skill's self-critique relies on.
- Vault `Resources/Production_Standards_AI_Generation.md`,
  `Automated_Reel_Assembly_Pipeline.md` — models, music, and final assembly.
