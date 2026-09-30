---
name: cm-lane
description: Community-Management (CM) lane knowledge for Era's brand/social dispatch — vault knowledge-source precedence, the Obsidian vault layout, invariants (brand isolation, degrade-never-raise, rules-outrank-instances, verbatim lessons, two approval gates), Era's vault write path, the feedback capture loop, nightly vault maintenance, the day-log + marketing-skill split, background-music generation for Reels, and Meta Ads dispatch. Use before touching era_dispatch.py, core/cm_vault.py, core/cm_learning.py, core/vault_maintenance.py, core/vault_writer.py, core/daily_note.py, music_generate.py, or anything under obsidian_era/era_brain/.
---

## Community-Management (CM) lane — how Era learns the user's workflow

Era's brand/social work runs through `era_dispatch.run_task()`, which spawns a
headless `claude -p` under the brand's own account. `build_brief()` composes the
prompt. **Four knowledge sources feed it, and their precedence is fixed:**

```
Brand Lock (brands.yaml routing)  >  brand charter  >  vault notes  >  learned lessons
```

| Source | Path | Nature |
|---|---|---|
| `brands.yaml` | repo root | Routing seatbelt + hard design gates. A wrong value is the only way a post reaches the wrong account. |
| **Obsidian vault** | `obsidian_era\era_brain\` | **The only source of truth for the CM.** Strategy the user wrote, *and* the brand charters (imported 2026-07-31). Read by `core/cm_vault.py`. |
| **Learned lessons** | `memory/cm_lessons.json` | Preferences Era *inferred* from corrections. Written by `core/cm_learning.py`. |

The charters used to live in each brand's `project_dir`. They are in the vault
now (`cm_vault.charter_path()`), and the brief tells the CM that any copy left in
a project folder is stale. `brands.yaml` stays outside the vault deliberately —
it is a safety mechanism, not a second memory.

### Vault layout (jaredrhod AI Memory Vault, migrated 2026-07-31)

```
era_brain/
  VAULT-INDEX.md        operating manual + brand profile
  Active Priorities.md
  00 - Inbox            unfiled — never briefed
  01 - Daily Notes      one dated log per day — never briefed
  02 - Delta Cuisine    charter, rules, campaigns, logs
  03 - Ceram Quartz
  04 - Global           rules true for both brands
  05 - Archive          never briefed
  06 - Resources        Marketing/ and other non-brand reference
```

Boot config: `D:\era_second_brain\CLAUDE.md` (outside the vault, per jaredrhod).
`cm_vault` still recognizes the old PARA folder names so the migration was
reversible; `scripts/migrate_vault_to_jaredrhod.py` is the (idempotent) migration.

### Invariants — do not regress these

- **Brand isolation.** A note or lesson belonging to one brand must never reach
  another brand's brief. Ownership is claimed by frontmatter `brand:`, by a
  brand **tag**, or by the filename — all three, not just `brand:`. Same class
  of failure as posting to the wrong account, and it fails silently.
- **Degrade, never raise.** A missing vault, unreadable note or corrupt lesson
  store costs context, never the job. Every call site wraps in try/except.
- **Rules outrank instances.** Vault notes are ordered by frontmatter `type:`
  *first* (`reference` > `guide` > `plan`), then brand-specificity. Ranking
  specificity first lets verbose campaign notes starve out standing rules. This
  used to key off the PARA folder name, which stopped working once folders became
  per-brand — everything Delta Cuisine owns now shares one folder.
- **What reaches a brief is decided in code, not convention.** `type: log` /
  `type: index`, `era_brief: false`, `status: archived`, or living in
  `00 - Inbox` / `01 - Daily Notes` / `05 - Archive` all mean never briefed. See
  `VaultNote.briefable` — `vault_audit` delegates to it rather than modelling the
  rule twice.
- **Lessons are the user's own words, verbatim.** Never paraphrase — that is how
  a preference mutates into something they never asked for.
- **Two gates still apply.** Nothing in the vault or lessons is an approval.
  `(approved)` in a filename is the user's signature; Era never writes it.

### Era's own write path into the vault

`actions/vault_writer.py` backs the **`vault_note`** tool — Era's only way to
write the vault while talking to you. Before it she could read the vault but not
write it: every memory tool (`save_memory`, `record_feedback`,
`manage_preference`) writes a JSON store under `memory/`. The symptom was
concrete — asked to interview her user and fill in VAULT-INDEX's Profile, she ran
the interview and had nowhere to put the answers.

It owns frontmatter and placement, because those are what decide whether a note
reaches a brief and they fail silently. Appends to an existing note before
creating one; `section=` replaces one `## heading` in place (the Profile case).
Refuses rather than guesses on an unrecognized brand, and refuses Era's own
regenerated notes (run log, learned preferences, vault health) — a hand-written
line there is destroyed by the next nightly pass.

### Capture loop

`run_task()` records feedback **before** dispatch (so it survives a crash) on two
conditions: the lane is `revise`/`approve` (judgement by definition), **or**
`cm_learning.carries_judgement()` finds a correction/approval marker in the order
itself. The lane alone was too narrow — "prepare this week's plan, and stop using
the clinical white kitchens" arrives as `weekly`, and that standing rule was
thrown away. Measured 2026-07-31: **two** lessons in the store's entire life.
`carries_judgement` is the discriminator that keeps routine orders ("plan the
week", "publish now") from burying the real ones — `classify()` cannot do it,
because it defaults polarity to "avoid" when nothing matched.

After each run, the outcome is appended to
`<brand folder>/Era_Run_Log_<brand>.md` in the vault, plus a dated session block
in `01 - Daily Notes/` (the cross-brand view no per-brand log can give).

Repeated feedback merges by Jaccard token overlap with the item number stripped
("change reel 1's texture" and "change reel 3's texture" are one lesson), so
saying something four times reads as a firm rule rather than four suggestions.

### Vault maintenance (automated, nightly)

`core/vault_audit.py` finds what is wrong. It contains **no write path at all**,
and it is the only one of the two the doctor imports — so a diagnostic can never
mutate the vault. `core/vault_maintenance.py` acts on those findings: repairs
what has exactly one right answer, rewrites the learned-preference notes, and
records the rest in `Areas/_Global/Era_Vault_Health.md`. It runs at 03:10 via
`era_scheduler` (`brands.yaml` → `defaults.schedules.vault_maintenance`, the
first entry in the new global/non-brand schedule slot).

Every failure it catches is **silent** — nothing raises, nothing logs, the
brief just quietly loses a rule. A `brand:` typo excludes a note from every
brief; a note past `_MAX_NOTE_CHARS` is truncated mid-sentence in every run; a
brand over `_MAX_BRIEF_CHARS` loses whichever note sorts last.

- **Never deletes user content.** The strongest action is a move into
  `Archive/`, and only on a `status: done` the user wrote. Age alone is
  reported, never acted on — a settled strategy note and an abandoned one look
  identical to a timestamp. (It does remove its own regenerated
  `Era_Learned_Preferences_` note when a brand runs out of firm lessons.)
- **Never guesses.** Auto-fix runs only where the vault's own structure answers
  the question (a `brand:` the folder already dictates). A folder/frontmatter
  disagreement is an error for a human, because picking one is exactly how
  cross-brand contamination happens.
- **`era_brief: false`** in frontmatter keeps a note in the vault but out of
  briefs. Era's own generated notes use it so they can't double-inject content
  the brief already carries from `cm_learning`.

### Day log (`Logs/`) and the marketing skill

Two additions from jaredrhod's AI Memory Vault / AI Marketing Skills
(CC BY-NC-SA 4.0), fitted to Era rather than copied:

- **`core/daily_note.py`** writes one dated note per day into `era_brain/Logs/`,
  appended after every CM run alongside the per-brand run log. It answers "what
  happened today, across both brands" — which no per-brand log can. `Logs/` is in
  `cm_vault._PARA_ORDER` (indexed, findable) but **not** in `_BRIEF_FOLDERS`, so
  it costs zero brief budget. That split is the whole mechanism.
- **`jaredrhod-marketing` skill**, installed in `~/.claude/skills/`,
  `~/.claude-delta/skills/` and `~/.claude-freelance/skills/` — every lane that
  does marketing work, including headless CM and ads runs. The same files sit in
  `era_brain/Resources/_Global/Marketing/` marked **`era_brief: false`**: readable
  in Obsidian, never in a brief. ~130 KB in the brief path would have silently
  pushed out Delta Cuisine's own rules at 85% budget. Editing the vault copy does
  NOT update the skill copies.

Authority is unchanged: `brands.yaml` > brand charter/vault > general marketing
fundamentals. The playbooks assume info-product funnels; both brands sell
high-consideration physical goods through a showroom, so the principles carry but
the funnel mechanics need translating.

### Inspect it

```bash
python -m core.cm_vault                        # vault connection + note counts
python -m core.vault_maintenance               # consistency audit, report only
python -m core.daily_note                      # day-log reachability
python -m core.vault_maintenance --apply       # + perform the safe repairs
python -m core.cm_learning show <brand_key>    # what the next brief will carry
python -m core.cm_learning promote <brand_key> # markdown, to paste into a vault note
python tools/era_doctor.py                     # includes all three checks
python -m pytest tools/test_cm_knowledge.py tools/test_vault_maintenance.py -q
```

### Background music (Reel production)

`music_generate.py` (repo root) calls Google Lyria RealTime directly (NOT
Magnific — rejected 2026-07-20) via `google-genai`, using a **dedicated**
`gemini_music_api_key` (`core/utils.py::get_music_api_key()`) so content
production never competes with Era's own voice-quota key. Needs
`http_options={"api_version": "v1alpha"}` — the default v1beta 404s the
endpoint; only discoverable by a live probe, not from the SDK's signatures.
Live-tested 2026-07-20. Full pipeline (images → clips → concatenate → score →
ffmpeg mux, no Premiere): vault `Resources/Automated_Reel_Assembly_Pipeline.md`.
Offline test suite: `tools/test_music_generate.py` (live Lyria call is a
manual opt-in via `ERA_LIVE_TESTS=1`, never runs in the normal suite; the key
itself resolves through `get_music_api_key()`, config first).

### Meta Ads (paid campaigns)

`task_type="ads"` is a **separate brief** (`era_dispatch._build_ads_brief()`),
not a section of the organic-content one — the organic brief's "never stop to
ask" instruction directly contradicts the ads mandate to ask for the end date
and budget on every campaign. `run_task()` refuses when
`brands.<key>.ads.enabled` is false, and always routes ads tasks to
`defaults.ads_claude_config_dir` (the freelance account) regardless of which
brand — that account is the only one with the `facebook-ads` MCP's OAuth
token. Confirmed live 2026-07-20: **an already-"connected" claude.ai
interactive connector does NOT carry into headless `claude -p` runs** — only
an explicitly-registered MCP server does; verify headless reachability with a
direct probe, never trust the `/mcp` panel alone. Test coverage:
`tools/test_ads_dispatch.py`. Full detail: `fable_meta.md`,
`.claude/skills/era-ads-manager/SKILL.md`.

Brand-knowledge overview for humans: `D:\my workflow\CLAUDE.md`.
Voice/text command reference: `docs/CM_COMMANDS.md`.
