# Starting a Hermes session on Era One

Hermes CLI (profile `era_one`) can edit the repo. It does NOT remember the last session, so
every session starts with the same paste. Do ONE phase task per session.

## Before the first session (once)
1. Restart the Hermes gateway so it sees the fixed master prompt.
2. In Hermes, type: `list the ## section numbers in docs/ERAONE UNIFIED MASTER PROMPT.md`
   It must include 3.2, 12 and 13. If not, the file is not the full one.
3. Stop Era before any session that runs pytest (the live Era holds the test lock).

## Paste this at the start of EVERY session
Replace the line starting `TASK:` and nothing else.

```
You are modifying the Era One repo at C:\Users\nesri\OneDrive\Documents\EraOne (Windows, Python 3.14, no venv).

READ FIRST, in this order, then reply with a 5-line summary of current state before touching anything:
1. CLAUDE.md
2. TODO.md
3. the LAST section of handoff.md
4. docs/ERAONE UNIFIED MASTER PROMPT.md -> Sections 0, 6 and 12, plus only the section your task names

TASK: <ONE task, e.g. "Phase 0: write docs/AUDIT_<today>.md, no code changes">

RULES
- Audit before changing: say what already works; do not rewrite working code.
- Before editing a file, copy it to .backups/<timestamp>/. After editing run:
  python -m ruff format . ; python -m ruff check --fix . ; python -m pytest tools/ -q
  plus a startup smoke test. If anything fails, restore the backup and report the exact error.
- Never touch config/api_keys.json, .env*, memory/*.json. Never print or log a key.
- No new files in the repo root. Keep files under 500 lines. Never delete 0 KB files.
- Model traffic goes through OmniRoute at 127.0.0.1:20128, combo era-gateway.
  Env vars are ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_MODEL. There is no
  OMNIROUTE_BASE_URL or OMNIROUTE_API_KEY, so do not report them as missing.
- Voice as built: Fish Audio (model s2.1-pro-free) is primary TTS, Gemini is the fallback.
  Do not reverse this.
- No git commit or push without my approval.
- Never say "done" without showing the verification output.

WHEN FINISHED: append a dated entry to handoff.md (what changed, what is verified, what is open),
tick the item in TODO.md, and list anything that is blocked on me.
```

## Phase order (one per session, do not skip)
| Session | TASK line to use | Done when |
|---|---|---|
| 1 | `TASK: Phase 0 audit, NO code changes to existing files. (1) Write docs/AUDIT_<today>.md with four buckets (implemented+working / implemented+broken / partial / not implemented) for each capability in Sections 1-5 of the master prompt, citing file path and covering test. (2) Run the baseline: python -m pytest tools/ -q with Era stopped, and list every failure. (3) Verify OmniRoute: with the ANTHROPIC_* env vars, make one short call to combo era-gateway from a terminal and record the model that answered and the latency. Never print the key. (4) Measure Fish TTS speed using core/tts_fish.py and tools/fish_audio_check.py: for the same 3 test sentences, 10 runs each, record time to FIRST audio byte (p50 and p95) for latency=low, balanced and normal, and a 15-word vs 3-word sentence. Also measure how long Era waits from Gemini text arriving to the first Fish request. Report whether the delay is Fish, the network, or Era's sentence handling, and propose fixes (for example WebSocket streaming, paid s2.1-pro, sentence-chunk size) without applying them. Finish by listing the P0 blockers in priority order.` | Audit file exists, baseline failures listed, Fish first-audio numbers recorded |
| 2 | Fix the P0 blockers from the audit: vision timeouts (`visual_engine`), Hermes gateway `TimeoutError` | Tests pass, timeouts reproduced then gone |
| 3 | Phase 2a: Fish `s2.1-pro-free` check + Fish -> Gemini -> Kokoro fallback cascade, chaos test | Failure of each backend falls through in under 1 s |
| 4 | Phase 2b: voice-command backend switching + barge-in tuning | Switch phrases work, no misfires on noise |
| 5 | Phase 3: pick 3 workflows, make each deterministic with preview + verification | Each workflow has a verification event |
| 6 | Phase 4: skill/plugin acquisition (voice -> search -> vet -> approve -> install) | End-to-end test passes, nothing installs without approval |
| 7+ | Phase 5 only if justified (always-on / cloud) | Recovery and cost-ceiling tests pass |

## Each new session, in 3 steps
1. Open the Hermes terminal, paste the block above with the next row's TASK line.
2. Wait for its 5-line state summary. If it contradicts `TODO.md`, stop and correct it.
3. At the end, check `handoff.md` got an entry. If not, tell it: "append the handoff entry now."

## If a session goes wrong
- Backups are in `.backups/<timestamp>/`. Ask Hermes to restore them, or tell Claude Code to.
- Hermes drifts on long sessions: start a new one and paste the block again.
