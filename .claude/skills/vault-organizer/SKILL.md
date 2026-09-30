---
name: vault-organizer
description: Files anything into Era's Obsidian vault (obsidian_era\era_brain) so it is never lost and always reaches the right brand. Use whenever the user says "remember this", "save this", "note that", gives a standing rule or correction, or asks to organize/clean up/tidy the vault — and BEFORE creating, moving or renaming anything under era_brain/. The vault is the ONLY source of truth for the community-management lane, and what reaches a CM brief is decided in code by frontmatter, so a note filed wrong is a note Era never reads.
---

# Vault Organizer — Era's second brain

The vault at `C:\Users\nesri\OneDrive\Documents\obsidian_era\era_brain` is the
**only source of truth for Era's community management**. `core/cm_vault.py` reads
it fresh before every CM job. Filing is not tidiness here: a misfiled note is one
Era never reads, and a mis-tagged note can leak one brand's strategy into the
other's brief.

Layout is jaredrhod's AI Memory Vault (github.com/jaredrhod/ai-memory-vault).
The operating manual is `VAULT-INDEX.md` at the vault root — read it if a
judgment call is not covered here. The boot config is `D:\era_second_brain\CLAUDE.md`.

> **Era is not Zoro.** `D:\Zoro` / `D:\Zoro brain` is a separate project with its
> own agent and vault. Never read from it, never write to it, never cite it.

## The structure

```
era_brain/
  VAULT-INDEX.md        the operating manual + brand profile
  Active Priorities.md  everything open, both brands
  00 - Inbox            unfiled capture — NEVER briefed
  01 - Daily Notes      one dated log per day — NEVER briefed
  02 - Delta Cuisine    everything Delta Cuisine
  03 - Ceram Quartz     everything Ceram Quartz
  04 - Global           rules true for BOTH brands
  05 - Archive          finished work — NEVER briefed
  06 - Resources        reference that is not brand knowledge (Marketing/)
```

Each folder has an index note named after it (`02 - Delta Cuisine.md`). **The
index is a contract:** update it in the same pass as any note you create, move,
or materially change. A stale index makes a future session decide from a wrong
map.

## Saving something the user just told you

This is the common case. "Remember X", "from now on Y", "never do Z again."

1. **Decide the folder by WHO it is about.** One brand → `02` or `03`. Both
   brands → `04 - Global`. Not about a brand at all → `06 - Resources`. Genuinely
   unsure → `00 - Inbox`, and say you put it there.
2. **Decide `type:` by WHAT IT IS.** This is the field that decides whether Era
   reads it, and in what order:
   - `reference` — a standing rule or library. **Ranked first in every brief.**
     A preference, a material list, a "never do X" — this is almost always the
     right answer for something the user just told you.
   - `guide` — a runbook or prompt system. How to do a thing, reusably.
   - `plan` — one campaign, batch or weekly plan, with an end.
   - `log` / `index` — **never briefed.** History and navigation only.
3. **Append before you create.** Find the existing note that is the logical home
   and add to it. Fewer, fuller notes beat many thin ones. A new note only when
   nothing existing fits.
4. **Write it as an instruction, not a diary entry.** "Never mix two stone
   families in one render" lands. "We noticed mixing stones might look busy" does
   not — the CM reads these the way it reads the charter.
5. **Update the folder index** and confirm to the user where it went.

## Frontmatter — the part that actually matters

```yaml
---
status: active            # active | completed | parked | idea | archived
project: delta-cuisine    # delta-cuisine | ceram-quartz | meta
type: reference           # index | reference | guide | plan | log
brand: delta_cuisine      # brand notes ONLY — omit entirely for global notes
---
```

Never ask the user for these. Infer them.

**`brand:` is the one to get right.** It is the strongest signal Era's
brand-isolation check reads. Set it explicitly on every brand note even when the
folder and tags already imply it. Note the two spellings are different on
purpose: `project:` is hyphenated (jaredrhod's convention), `brand:` uses the
underscore (Era's brand key).

**An unrecognized `brand:` value excludes the note from EVERY brief.** A hyphen
where an underscore belongs, a display name instead of the key — the note simply
stops existing as far as the CM is concerned, silently. The nightly audit catches
it; do not create it.

**Never put a brand's notes in the other brand's folder**, and never duplicate a
note into both — a note that covers both brands belongs in `04 - Global`.

## Two silent limits to respect

- **4,500 characters per note.** Past that the tail is cut from every brief,
  mid-sentence, with no error. If a note is outgrowing it, split along the seam:
  standing rule → `reference`, reusable procedure → `guide`, this campaign's
  specifics → `plan`.
- **55,000 characters per brand, total.** Over that, whichever note sorts last is
  dropped. Keep `era_brief: false` on anything long that the CM can open on
  demand instead — the charters and the Marketing folder already do this.

## What never to put in the vault

- **Account IDs, page IDs, budget caps, footers.** `brands.yaml` owns routing and
  outranks the vault on it, always. A second copy here only drifts.
- **Approvals.** `(approved)` in a filename is the user's signature at Gate 2.
  Writing "approved" in a note does nothing; both gates still apply.
- **Secrets.** OneDrive-synced, unencrypted.

## What never to touch

- **`Era_Run_Log_<brand>.md`** — Era appends under `## Run log`. Read and
  annotate freely; never edit above that heading, never move it out of the brand
  folder (the path is derived in `cm_vault._log_note_path()`).
- **`Era_Learned_Preferences_<brand>.md`, `Era_Vault_Health.md`** — rebuilt from
  scratch nightly. Any edit is overwritten.
- **A note that looks hand-written and was just touched** — likely the user
  mid-edit. Ask before refiling.
- **`.obsidian/`** — plugin state, never content.

## Renaming and archiving

**Renaming breaks every `[[link]]`** to a note unless done inside the Obsidian
app. Do renames in the app; if you must rename a file directly, find and fix
every reference in the same pass. Moving between folders is safe — wikilinks
resolve by name.

**Archiving** is `status: archived` plus a move to `05 - Archive/<Brand>/`.
Confirm first; never archive on your own initiative. Nothing is ever deleted.

## Tidy-up — start with the audit, do not redo it by hand

`core/vault_audit.py` checks everything mechanical and runs nightly at 03:10.
Do not re-derive its findings by reading files one by one:

```bash
python -m core.vault_maintenance            # report only
python -m core.vault_maintenance --apply    # + the two safe repairs
```

It auto-fixes exactly two things — adding a `brand:` the folder already dictates,
and archiving a `status: done` plan. Everything else it reports, because
everything else needs judgment:

1. **`brand_folder_mismatch`** — folder and frontmatter disagree. Only the user
   knows which is right. Ask; never pick one.
2. **`note_truncated`** — split it along the rule/procedure/instance seam.
3. **`brief_budget_pressure`** — archive concluded plans, or set `era_brief:
   false` on something the CM can open on demand. Prefer that to raising the cap:
   a brief past a certain size starts losing the gates.
4. **`aging_project`** — ask if it is finished; if yes add `status: done` and let
   the next pass file it.

Then verify with the doctor, not by eye:

```bash
python -m core.cm_vault                  # reachability + note counts per folder
python -m core.cm_vault delta_cuisine    # the rendered brief for one brand
python -m core.cm_vault ceram_quartz
```

Read each brand's block and confirm no note from the other brand appears, and
nothing you expected is missing.

Run `python -m pytest tools/test_cm_knowledge.py tools/test_vault_maintenance.py tools/test_daily_note.py -q`
if you changed `core/cm_vault.py`, `core/vault_audit.py` or `core/daily_note.py`
themselves. Not needed for a pure filing pass — those tests use a synthetic tmp
vault and never touch the real one.
