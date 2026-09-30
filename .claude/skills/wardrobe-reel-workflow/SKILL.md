---
name: wardrobe-reel-workflow
description: The master-anchored production method for Delta Cuisine modular-wardrobe Reels and image sets — the fix for shots that drift (layout changes between shots, missing doors, swapped/generic accessories, a different wardrobe in shots 2-3, unreal renders). Use BEFORE generating ANY wardrobe Reel or multi-shot wardrobe image set. It replaces the old "6 independent prompt nodes in the Magnific Space" approach — which cannot hold identity across shots — with a sequential pipeline that generates ONE master wardrobe and DERIVES every other shot from that image (change-camera / image-reference), never re-generating from text. It does NOT replace the brand's hard rules (brands.yaml delta_cuisine.design_rules.wardrobe is authoritative on layout, doors, accessories, materials) or the creative bar (reel-art-director); it is the MECHANICAL method that finally makes those rules hold from image to video. If this and brands.yaml disagree, brands.yaml wins.
---

# Wardrobe Reel Workflow — master-anchored generation

## The one idea that fixes everything

A wardrobe Reel is **six views of ONE wardrobe**, not six wardrobes. The old
Space generated each shot from its own text prompt, so every shot was a fresh
roll of the dice — a different door count, a different accessory, a re-imagined
layout. No prompt, however detailed, can make two independent generations
identical. That is the root cause of every drift Karim reported.

**The fix: generate the wardrobe ONCE (the master image), then DERIVE every
other shot FROM that image.** Angle shots come from re-aiming the camera at the
master; door-open and accessory shots come from generating with the master as an
image reference. Nothing after the master is generated from text alone. Identity
is inherited, not re-rolled.

> Brand rules stay in `brands.yaml → delta_cuisine.design_rules.wardrobe`
> (reference_lock, door_identity, facades, module_contents, accessories,
> ordered_slots, reel_styles). This skill does not restate them — it is HOW to
> execute them so they survive from the first image to the final video. Read
> them, and the `interior-design` + `reel-art-director` skills, before starting.

## The pipeline (run it in order; QC gate between every step)

### 0. Plan the concept (Gate 1 — before any credits)
Pick ONE `allowed_configuration`, ONE conception_reference image to follow, the
material(s) from the swatch library, and the accessory set (the `starax hanger
lift` is mandatory + exactly TWO others). State door count and each accessory's
module position. This is the plan Karim approves. **Generate nothing yet.**

### 1. Generate the MASTER — the fully-open wardrobe (the identity anchor)
`images_generate`, aspectRatio `9:16`, following every wardrobe rule. The master
is the **fully-open** wardrobe with everything visible: doors open on their
hinges (visible European cup hinges), the couple's hanging section, the drawers,
and the chosen accessories in their stated modules (belt/tie rack, pull-out
racks, and the starax lift shown mid-travel). Attach the conception_reference
image(s) and the real accessory photos from `Wardrobe premium accessories/` as
`references` so the layout and the hardware are REAL, not invented.

**QC the master hard (this is the most important gate).** Download and VIEW it.
It must pass every brand rule AND be beautiful — because every other shot
inherits it. If the master's doors, accessories, or proportions are wrong, fix
the MASTER now (re-generate / re-prompt). Never derive shots from a flawed
master; you would just multiply the flaw six times. Two re-rolls; if still wrong,
show Karim the master alone and ask before spending on the derived shots.

### 2. Derive the supporting shots FROM the master (never from text)
Map each `ordered_slots` entry to the technique that PRESERVES the master:

| Slot | Shot | How to derive it from the master |
|---|---|---|
| 1 | WIDE hero, all doors CLOSED, straight-on | `images_generate` with the master as an `image` reference + prompt "same wardrobe, all doors closed, straight-on, uninterrupted tall facades". (Or shoot the closed hero as its own master and open-master as the reference — either way one is derived from the other so they match.) |
| 2 | CLOSE door line / handle / shadow gap | `images_change_camera` on the master: high `closeup` (8-10), small `rotate`, tight on one vertical door edge |
| 3 | WIDE three-quarter, ONE door open on its hinge | `images_change_camera` (rotate ~30-45, closeup ~4) to a three-quarter, OR `images_generate` referencing the master with "one door open on its hinge, panel in frame" |
| 4 | CLOSE pull-out accessory in motion | `images_generate` referencing the master + the accessory photo, prompt "the <accessory> gliding out", tight framing — a DIFFERENT subject from slot 2 |
| 5 | WIDE low angle up the vertical | `images_change_camera`: `vertical` negative (down-top), closeup low, full height |
| 6 | CLOSE top-down into an open drawer | `images_change_camera`: `vertical` high (looking down), closeup high, on the open drawer |

`images_variations` mode `angles` on the master is a fast way to get several
identity-locked angles at once; pick the tiles that match the ordered slots.

**QC each derived image against the master (reference_lock check):** identical
finish/colour, identical door count and width, identical accessory in the
identical module, identical room/props. Any drift = re-derive that ONE shot
(never accept it as a "variation"), up to two re-rolls. Enforce the WIDE/CLOSE
alternation and the "no two slots share a subject" rule.

### 3. Animate each locked image (image -> video)
`video_generate` per shot, **model `kling-25` (Kling 2.5), 5s, 9:16, 720p**
(Karim's policy — unlimited-friendly; validated to hold identity with no morph).
At 720p Kling requires `keyframes.start` (= that shot's image creation
`identifier`, NEVER a webUrl) and forbids an end keyframe — exactly our flow.

**CAMERA-ONLY MOTION — Karim's hard rule (2026-07-22).** NOTHING in the shot may
move: the wardrobe, doors, drawers, hanging clothes and accessories stay perfectly
still, frozen exactly as the master image. The ONLY motion is a slow cinematic
**camera** move — push-in, pull-out, orbit, glide, or tilt — matching the ONE
chosen `reel_style` (cinematic-slow OR modern-montage) and consistent with
`montage.motion_handoff` so clips continue each other. Write the prompt as a
**camera** instruction ("camera slowly pushes in, everything in the scene stays
static"), never as an action ("door opens", "drawer glides out") — an action verb
is what makes Kling animate the object and break identity. QC: extract the mid +
end frames (ffmpeg) and view them — if the wardrobe warps, melts, grows/loses a
door, gains a phantom accessory, OR any object in the scene moves, the clip is
REJECTED; re-generate with a calmer, purely-camera move.

### 4. Combine + score
`video_concatenate` in the ordered_slots order (1..6). **Music: NEVER Magnific
`audio_music_generate` (expensive, banned by Karim).** Generate the background
track with the repo's free `music_generate.py` (Google Lyria on the dedicated
music key), matched to the chosen `reel_style`, and mux it under the concatenated
Reel with ffmpeg. Then apply the `reel-art-director` self-critique bar to the
WHOLE Reel (the SELF-CRITIQUE GATE also lives in the dispatch brief). If it
passes, it is a draft awaiting Karim's Gate-2 approval — never write "(approved)"
yourself.

## Model + resolution policy (cost split — Karim, 2026-07-22)
- **Master:** `imagen-nano-banana-2` (Nano Banana Pro) at **2K**. It is the
  identity anchor and the only 2K image — spend the fidelity here.
- **Derived shots 1-6:** `imagen-nano-banana-2-flash` (Nano Banana 2 Flash) at
  **1K** for any `images_generate` (door-open, accessory, closed hero). Flash 1K is
  covered by the unlimited plan; Nano Banana Pro is not. `images_change_camera` has
  no model choice — it inherits the master. **Never** spend 2K / Nano Banana Pro on
  a derived shot. This makes a full Reel almost free once unlimited is active,
  charging real credits only for the one master image.

## Why this is better than the old Space
- **Identity is inherited, not re-rolled** — the #1 fix for "a different wardrobe
  in shots 2-3", missing/extra doors, swapped accessories.
- **QC between every step** — a flaw is caught on the master or the single derived
  shot, before it is multiplied or animated. The old Space fired all nodes at once
  with no mid-flight check.
- **Credits spent in order of risk** — the master is validated before any derived
  shot or video is paid for.

## Any wardrobe type, same method
This works for all three `allowed_configurations` and any conception_reference:
the configuration and reference only change what the MASTER looks like. The
derive-don't-regenerate method is identical every time — that is what makes it a
reusable workflow rather than a one-off.

## When to fall back
If `images_change_camera` / image-reference cannot hold a particular shot (rare —
usually a very tight macro), generate that ONE shot in the Magnific Space with the
master attached as a `creation` reference node, QC it against the master, and
continue. Do not abandon the whole method for one hard shot. If the master itself
cannot be made right after two tries, STOP and show Karim — never ship or animate
a wrong master.

## Related
- `brands.yaml → delta_cuisine.design_rules.wardrobe` — the authoritative hard rules.
- `.claude/skills/interior-design/SKILL.md` — material-harmony + pre-generation checklist.
- `.claude/skills/reel-art-director/SKILL.md` — the creative/quality bar and self-critique.
- `.claude/skills/magnific-space-expert/SKILL.md` — the fallback Space mechanics.
