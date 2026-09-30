# Memory & Feedback Cleanup — Findings and Execution Plan

_Author: Claude (for Karim) · Date: 2026-07-09_

This note is the record requested for cleanup execution. It (1) explains the root
cause of the "Not saved" failures and the "feedback is ignored" problem, (2)
groups the 220 raw feedback entries by category, (3) lists every duplicate and
contradiction found, (4) states the single resolved intent for each conflict, and
(5) describes the new memory configuration that keeps everything findable and
prevents future accumulation.

Items marked **[CODE — DONE]** are already applied in this repo. Items marked
**[DATA — SCRIPT]** touch guarded files (`memory/long_term.json`,
`memory/feedback_memory.jsonl`) and are applied by running
`scripts/cleanup_memory.py --apply` (it backs up both files first).

---

## 1. Root causes

| Symptom | Real cause | Fix |
|---|---|---|
| "Not saved" on save | Intentional security gate: a save only commits when the turn contains an explicit memory trigger phrase. Natural phrasings ("keep that in mind", "you must know") weren't in the trigger list. | **[CODE — DONE]** `_MEMORY_TRIGGERS` in `main.py` broadened to natural voice phrasings. |
| "You never remember my feedback" | `user_feedback` was **never rendered** into the prompt (missing from `category_configs`), and the recency top-15 / 2000-char cap silently dropped standing operational rules as newer chatter arrived. | **[CODE — DONE]** `format_memory_for_prompt_ranked` now **pins** durable categories and renders `user_feedback`. |
| Feedback not obeyed even when present | No directive told Era that feedback outranks its defaults. | **[CODE — DONE]** "FEEDBACK IS AUTHORITATIVE" directive added to `core/prompt.txt`. |

---

## 2. The new memory configuration (anti-accumulation)

Two tiers, so growth never buries the rules:

- **Tier 1 — Durable (PINNED, always injected in full):** `identity`,
  `operational_instruction`, `developer_feedback`, `user_feedback`. These are
  rules and corrections. They are rendered every session in a fixed logical order
  and can **never** be evicted by newer entries. This is what guarantees "Era
  finds everything clearly and fast" and "no accumulation problem."
- **Tier 2 — Ephemeral (recency-ranked, trimmable):** `preferences`,
  `relationships`, `projects`, `favorite_paths`, `notes`, `wishes`. These share a
  recency budget and are the only lines trimmed if the 4000-char cap is hit.
- **The raw `feedback_memory.jsonl` is an append-only audit journal, not runtime
  memory** — it is never read back during a session. Durable lessons from it are
  *promoted* (consolidated + deduped) into `user_feedback` /
  `operational_instruction`, which is what Era actually reads. This keeps the
  journal free to grow without ever becoming a retrieval problem.

Logical render order (top to bottom): Identity → Strict Operational Instructions →
Developer Patches → **User Feedback (PRIORITY)** → Preferences → People → Projects
→ Paths → Notes → Wishes.

---

## 3. Feedback grouped by category

Counts are approximate (220 raw lines; many are ASR fragments/acknowledgements).

| Category | What it covers | Representative lines |
|---|---|---|
| **Media — Spotify visibility** | minimize/hide after use; verify-first | 15, 27, 32, 40, 110, 129, 162, 163, 164, 167, 168, 178, 186, 206 |
| **Media routing** | Spotify=app, Netflix=web, don't cross-route, Google-not-Netflix search | 4, 67, 81, 82, 83, 90, 155 |
| **App launching** | Claude via search bar not terminal; Photoshop/Premiere 2026 not beta; Neat Download Manager; Facebook via Brave; open the exact app | 44, 45, 56–60, 92–107, 139–141, 152, 205 |
| **File handling** | append not overwrite issue reports; save to Documents/Downloads not project dir; make a file for each reported issue | 10, 187, 204, 210 |
| **Standby / wake** | only standby on explicit command; correct enter/resume phrasing; verbal notify | 13, 24–26, 30, 34, 52–54, 61, 62, 71, 76, 77, 116, 172, 176 |
| **Startup / greeting** | greet on new session; startup routine (only launch if not running) | 1, 5, 6, 63, 180, 181 |
| **Language / speech** | English permanent; Darija+French only on request; no accent; ignore unknown languages | 122, 123, 124, 125, 126, 207, 208 |
| **Clarification / safety** | multiple voices or unclear → ask, don't act | 28, 72, 198 |
| **Verification** | confirm task truly done (vision/state); don't ask "try again" | 68, 86, 87, 88, 164, 165, 193 |
| **Response style** | vary phrasing; 2–3 word confirmations; concise "Done" | 65, 169, 170 |
| **Identity / naming** | Karim; WhatsApp "Bachboucha"; caption material name (quartz) | 127, 128, 130, 153, 154 |
| **Brand / CM** | Delta Cuisine lock; Ceram Quartz; route to correct brand | 121, 135, 145 |
| **Son / favorite songs** | son's favorite = "I Am on Vacation"; favorite = "Berin - Reborn - Radio Edit" | 50, 194, 195, 197, 200, 201 |
| **Feature requests** | MSI Center scenarios; PC Boost; self-diagnostics | 120, 190, 191 |
| **System issues (bugs)** | mic on/off discrepancy; standby self-wake; close-confirm inaccuracy; UI flicker | 21, 46, 47, 74, 80, 116, 132, 202 |
| **Praise / noise** | "Perfect", "I like your voice", half-sentences — non-actionable | 7, 11, 33, 35, 36, 100, 151, 159, 182, 216 |

---

## 4. Duplicates (collapse to one)

- **Spotify minimize:** 15, 32, 40, 110, 129, 162, 163, 186 all say "minimize
  Spotify after use." → one rule.
- **Language:** 122, 124, 125, 126, 207, 208 → one rule.
- **Open Claude via search bar:** 92, 93, 96, 97, 98, 99, 103, 104, 105, 106,
  107 → one rule.
- **Photoshop 2026 not beta:** 44, 45, 59, 60 → one rule.
- **Reuse existing tab:** 42, 43, 214, 215 → one rule.
- **Verify task done:** 87, 88, 165, 193 → one rule.
- **Neat Download Manager:** 141, 205 → one rule.

## 5. Contradictions (resolved to a single intent)

1. **Close Spotify** — 136 ("don't close without authorization") **vs** 137/138
   ("close Spotify without confirmation").
   → **Resolved:** close Spotify and other no-risk apps **without** confirmation;
   all other apps still require authorization. (Matches
   `operational_instruction.close_spotify_no_confirm = true`.)
2. **Spotify minimize timing** — "minimize immediately/after use" (15, 32, 40,
   110, 129, 162, 186) **vs** "don't minimize immediately; verify playback first"
   (164, 167, 168).
   → **Resolved (Karim's decision):** **verify the action is actually done, THEN
   minimize** — the "Spotify verify-then-minimize" rule.
3. **Standby phrasing** — going offline should say "I'm offline" (52) but resuming
   should say "online" (53, 54, 172).
   → **Resolved:** entering standby → "offline / on standby"; resuming → "online".
4. **Son topic** — `avoid_son_topic` ("never mention their son") **vs** repeated
   requests to store and play the son's favorite song (50, 194, 197).
   → **Resolved:** **remove `avoid_son_topic`**; Karim actively wants the son's
   song remembered and playable.

---

## 6. Data cleanup actions — `scripts/cleanup_memory.py --apply`

**`memory/long_term.json`:**
- Remove `operational_instruction.avoid_son_topic`.
- Set `preferences.favorite_song = "Berin - Reborn - Radio Edit"`.
- Add `preferences.son_favorite_song = "I Am on Vacation"`.
- Add `operational_instruction.spotify_verify_then_minimize` (the single
  consolidated Spotify visibility rule); leave `close_spotify_no_confirm`.
- Add `operational_instruction.close_apps_policy` (resolves contradiction #1).
- Promote the deduped durable lessons into `user_feedback` so Era actually applies
  them (standby phrasing, verify-task-done, language, response style,
  reuse-open-tab, file-save-location, clarification-on-unclear).

**`memory/feedback_memory.jsonl`:**
- Back up to `feedback_memory.jsonl.bak-<timestamp>`.
- Rewrite regrouped: sorted by canonical category then time, exact-duplicate
  `user_text` collapsed, pure-noise/acknowledgement fragments dropped (still
  preserved in the `.bak`). The journal stays append-only afterward.

Run:
```bash
python scripts/cleanup_memory.py            # dry run — prints what would change
python scripts/cleanup_memory.py --apply    # writes, after backing up both files
```
