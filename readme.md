# Era One — Windows AI Assistant

Era One ("Era") is a voice- and text-driven AI assistant that runs locally on
Windows. She listens, talks back, controls the desktop and apps, manages a
real-time HUD, remembers what matters, and can run work in the background —
all behind a multi-level safety layer that keeps destructive or outward-facing
actions behind an explicit confirmation.

This README is the user manual: **how Era works**, **what she can do**, and
**how to use her**. Deeper operational and safety detail lives in
[docs/OPERATIONS.md](docs/OPERATIONS.md) and [docs/SAFETY.md](docs/SAFETY.md).

---

## 1. How Era works

Era is a single Python process (`main.py`) that wires together a voice/text
loop, a tool dispatcher, and a safety gate. Every request — however it
arrives — flows through the same pipeline:

```
  voice  ─┐
  typed  ─┤→  input safety gate  →  model (intent + tool choice)
  Telegram┘                               │
                                          ▼
                              action registry  →  tool dispatch
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                  confirmation       execute tool      structured log
                     gate            (desktop, web,    (era.log, task
                 (risk-based)         media, memory…)   trace, HUD)
```

- **One input path.** Typed text, transcribed voice, and Telegram voice notes
  all land in the same handler, so a safety check or memory write that applies
  to one applies to all.
- **The model decides intent; Era decides what is allowed.** The language model
  proposes a tool and arguments. Era — not the model — checks the risk level and
  whether the action may run. Authority comes from what the **user** actually
  said, never from the arguments the model echoes back.
- **Tools do the work.** Each capability (launch an app, search the web, play a
  track, read mail, remember a fact) is a tool resolved through an action
  registry and executed by the dispatcher.
- **Everything is logged.** Outcomes go to structured logs and a per-day task
  trace, and the live HUD reflects state as it changes.

### Voice & models

- Primary voice is Google **Gemini Live**; a local voice (**Kokoro**) and a
  **Claude** text/voice path act as fallbacks when Gemini is unavailable.
- Speech synthesis routes per sentence (Fish Audio primary, Gemini fallback).
- API quota is handled with **multi-key failover** (quota is per key, so Era
  rotates keys before cooling down) and, for text, a failover to Claude.
- Era is **half-duplex** by design (she listens or speaks, not both), with
  barge-in so you can interrupt her.

### Memory & learning

- Era keeps a two-tier memory plus **semantic recall** (recall by meaning, not
  just keywords).
- She learns from your corrections: feedback you give ("you spelled that wrong",
  "do it this way") is captured and applied later.
- **Your learned memory and feedback stay on your machine.** The data files
  (`memory/*.jsonl`, `memory/*.json`, organization snapshots) are gitignored and
  never pushed to the remote; only the memory *code* is versioned.

---

## 2. What Era can do

| Area | Capabilities |
|------|--------------|
| **Desktop control** | Launch and control apps, manage windows, change computer/audio settings, route audio between speakers and glasses |
| **Web & browser** | Drive a real browser (Playwright/CDP), run web searches, open sites, control **Netflix** (play, seek, profiles) and **YouTube** |
| **Media** | Spotify playback and search, system/app volume, background music generation for content |
| **Mail** | A Gmail lane (read, search, label, draft — sending is gated) |
| **Scheduling** | A built-in scheduler for one-off and recurring tasks, task chains, and conditional ("if"/"until") runs |
| **Memory** | Remember facts, recall by meaning, learn from feedback, truthful self-model ("what can you do?") |
| **Community management (CM)** | Brand/social content via an Obsidian "vault", Meta Ads dispatch, with brand isolation and approval gates |
| **Crew** | A multi-agent "crew" (research, build, review) that can take on larger jobs in the background |
| **Vision** | Look at the screen / camera when asked |
| **Self-maintenance** | Diagnose faults from its own logs and stage scoped, reviewable self-improvements |
| **HUD** | A local real-time heads-up display showing state, schedule, diagnostics, and conversation |

What Era **can do at any moment** is computed from her actually-enabled
capabilities, not from a prompt — so "what can you do?" reflects reality.

---

## 3. Quick start

```powershell
# 1. Setup
python setup.py

# 2. Configure API keys  (see config/api_keys.example.json)
#    Edit config/api_keys.json

# 3. Validate
python tools/run_all_checks.py      # "ALL_CHECKS_OK" / "READY" = good

# 4. Run
python main.py
```

**Prerequisites:** Windows, Python 3.12+ (the runtime here is 3.14). There is no
virtualenv in this setup; dependencies are installed into the system Python.

---

## 4. Configuration

### API keys — `config/api_keys.json` (gitignored)

```json
{
    "gemini_api_key": "your-gemini-key-here",
    "os_system": "windows",
    "SPOTIPY_CLIENT_ID": "your-spotify-client-id",
    "SPOTIPY_CLIENT_SECRET": "your-spotify-client-secret",
    "SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    "OPENAI_API_KEY": "your-openai-key-here"
}
```

Only `gemini_api_key` is required. Spotify, OpenAI and other keys are optional
and unlock their matching features. **Never commit this file** — it is
gitignored and also denied at the tooling layer.

### Useful environment toggles

- `ERA_CREW_TRANSPORT=hermes_agent` — route the multi-agent crew through the
  Hermes gateway; unset (or `setx ERA_CREW_TRANSPORT ""`) to use the in-process
  path.

---

## 5. Using Era day to day

- **Talk to her.** Speak naturally; she transcribes, acts, and replies by voice.
  Interrupt any time (barge-in). A bare "stop" does **not** trigger a panic stop
  — use the explicit abort phrase for that.
- **Type to her** for the same capabilities without voice.
- **Telegram.** Send text or voice notes through the Telegram bridge; voice
  notes are transcribed into the same safe input path.
- **Ask what she can do.** "What can you do?" returns a truthful, computed list.
- **Remember / correct.** "Remember that…", or just correct her — feedback is
  captured and applied later. These saves are a live preference you can toggle.
- **Schedule work.** "Every morning at 8…", "in 30 minutes…", "when X then Y" —
  one-off, recurring, chained, and conditional tasks are supported.
- **Hand off big jobs** to the crew (research → build → review) and let it run
  in the background; Era reports back when it finishes or fails.

### The HUD

Era renders a local, token-guarded heads-up display in the browser
(`127.0.0.1`) showing current state, the schedule, diagnostics, capabilities,
and the live conversation. It launches silently alongside Era.

> **Do not force-kill the Era process** to stop the HUD or Era — that kills the
> supervisor. Use Era's own reboot/shutdown commands.

---

## 6. Safety model

Era uses a multi-level safety system (full detail in [docs/SAFETY.md](docs/SAFETY.md)):

- **LOW** — executes immediately.
- **MEDIUM / HIGH** — requires explicit confirmation (e.g. sending a message,
  posting content, spending). Era asks and waits for *your* answer; she never
  confirms on your behalf.
- **BLOCKED** — denied outright (e.g. deleting system files).
- **Secrets are masked** everywhere — passwords, tokens and API keys never
  appear in logs, messages, or results.
- **Owner-only voice gate** — voice commands are gated to the owner and fail
  closed.
- **Panic stop / abort** — a dedicated abort path halts work through several
  model-independent channels.

Confirmation thresholds are adjustable standing preferences, but high-risk
categories stay gated to a live user.

---

## 7. Validate health

```powershell
python tools/run_all_checks.py
```

`ALL_CHECKS_OK` / `READY` means all systems are operational. Lint, format, and
tests gate every change:

```powershell
python -m ruff format .          # format
python -m ruff check --fix .     # lint + autofix
python -m pytest tools/ -q       # tests
```

---

## 8. Troubleshooting

- **Era "hangs" on startup / tests** — she may be waiting on the single Era lock
  held by a live instance. Only one Era runs at a time.
- **Voice connects but is silent** — usually an upstream model overload; Era
  falls back to the local/Claude voice path.
- **A capability says it's unavailable** — check that its API key is set in
  `config/api_keys.json` and that `run_all_checks.py` reports `READY`.
- See [docs/OPERATIONS.md](docs/OPERATIONS.md) for environment, dependencies,
  logs, and deeper troubleshooting.

---

## 9. Documentation

- [Operations Guide](docs/OPERATIONS.md) — environment, dependencies, memory, logs, troubleshooting
- [Safety Guide](docs/SAFETY.md) — risk levels, confirmation flow, logger safety, path protection

---

## License

Private project — by karimbenattig.
