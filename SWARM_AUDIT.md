# EraOne — Swarm Audit Report

*Generated 2026-07-02. Four parallel agents (security, test coverage, documentation, architecture) audited the codebase independently. The strongest signal is where they **converge** — several issues were flagged by three agents from different angles.*

## Verdict

EraOne is a capable, feature-rich Windows voice assistant with genuinely good bones in places — the safety/confirmation/verification pipeline is modular and well-tested, and `docs/SAFETY.md`/`OPERATIONS.md` are accurate. But it carries **one Critical security exposure that needs action today**, a cluster of **High-severity RCE/injection paths that partially defeat its own safety model**, and a **default test suite that silently collects zero tests** — meaning the "strong test culture" is providing less protection than it appears. The 128KB `main.py` god object and a tool system with four unsynchronized sources of truth are the root causes that make the other problems recur.

## Cross-cutting themes (independently corroborated)

**① Leaked live credentials — flagged by 3 of 4 agents.** `config/api_keys.json` holds real, in-use keys (Gemini ×2, OpenAI `sk-proj-…`, Spotify secret). Security rated it **Critical**; Documentation independently found the referenced `api_keys.example.json` is *missing* and the real file sits in its place; Architecture flagged secrets-in-repo. Aggravating factor: the folder is under **OneDrive sync**, and `SECURITY_ROTATION.md` already admits a prior leak. → **Rotate now.**

**② The safety model can be bypassed — Security + Architecture interlock.** Security found `desktop_control(task=…)` `exec()`s LLM-generated Python with `ctypes` in scope and **no confirmation** (falls through to default-LOW). Architecture independently found confirmation state is split across **two `PendingConfirmationStore` instances** (one in `EraLive`, one module-global in `agent/executor.py`), so a gate honored on one execution path doesn't exist on the other. Together these explain *how* a dangerous action reaches execution unconfirmed.

**③ Tool dispatch drift — Architecture + Test + Docs.** The tool set is declared/dispatched in four unsynchronized places; `tool_registry_validator.py` exists only to detect the drift (and the two dispatchers *have* already diverged). Test coverage confirms `agent/executor` and `main.py`'s dispatcher are essentially untested; Docs found the tool naming itself has drifted (Era One / MARK-XXXIX / jarvis).

**④ Tests give false confidence.** A bare `pytest` run **collects nothing** (a stray `sys.exit(0)` in `test_verify_changes.py` aborts collection), and `test_memory_safety.py`'s ~200 safety checks are `print`-based, not `assert` — so memory-guard regressions pass silently. 1692 real tests exist but only run under `pytest tools`.

## Consolidated findings, ranked

| # | Sev | Finding | Location | Source |
|---|-----|---------|----------|--------|
| 1 | Critical | Live API keys/OAuth secrets in plaintext, loaded at runtime, under OneDrive sync | `config/api_keys.json` | Sec, Doc, Arch |
| 2 | High | RCE: `exec()` of LLM code with `ctypes`/`pyautogui`, no confirmation | `actions/desktop.py:84` | Sec |
| 3 | High | Confirmation gate splittable / default-LOW fall-through lets tools bypass gating | `core/safety_guard.py:367`, two `PendingConfirmationStore`s | Sec + Arch |
| 4 | High | Command injection via `shell=True` taskkill from voice-supplied window title | `actions/window_control.py:234` | Sec |
| 5 | High | Bare `pytest` collects 0 tests; default CI signal is dead | `test_verify_changes.py:214`, `pytest.ini` | Test |
| 6 | High | Memory-safety "tests" don't assert — regressions pass silently | `tools/test_memory_safety.py` | Test |
| 7 | High | Tool set has 4 sources of truth; dispatch duplicated & already drifted | `main.py:178/1847`, `agent/executor.py:294` | Arch |
| 8 | High | `EraLive` god object: audio+wake-word+session+dispatch+safety in one ~1,480-line class | `main.py:944-2426` | Arch |
| 9 | High | Documented secrets-setup flow broken (`api_keys.example.json` missing) | README, `REPRISE_WINDOWS.md` | Doc |
| 10 | High | Entire vision/Ollama subsystem (~14 keys, 13 files) undocumented | `config/`, `core/vision_*` | Doc |
| 11 | Med | Autonomous dev-agent runs `pip install <llm-name>` + `shell=True`, no confirmation | `actions/dev_agent.py:249,271` | Sec |
| 12 | Med | `self_healing_router` (1,868 lines) rewrites intent via module-global state | `core/self_healing_router.py` | Arch |
| 13 | Med | `agent/` + `main.py` dispatcher essentially untested | — | Test |
| 14 | Med | `.checkpoints/` = stale, drifting copies of tests and production source | `.checkpoints/` | Test + Arch |
| 15 | Med | Deps unpinned; `setup.py` installs wrong requirements file; Windows file omits test deps | `requirements*.txt`, `setup.py` | Doc |
| 16 | Med | Dead `tools/jarvis_doctor.py` reference; 3 product names | `docs/OPERATIONS.md:159` | Doc |
| 17 | Low | SSRF (wallpaper-from-URL), `tempfile.mktemp` TOCTOU, `os.system` f-string, pervasive win32 coupling, no coverage tool, unused markers, mojibake in `main.py` | various | Sec, Arch, Test |

## What's genuinely good (don't regress these)

- **Confirmation & input-safety gates are real and well-tested** — TTL, thread-safety, replay/param-swap resistance; backup-failure *blocks* the write; protected system paths classified `BLOCKED`.
- **Destructive file ops are conservative** — `send2trash` for deletes, timestamped `.bak` backups before overwrite/move.
- **The "live" Netflix tests are actually mocked** — CI-safe; no test hits real browser/Netflix/Spotify.
- **Cross-cutting concerns are cleanly modularized** around a `ToolResult` value object; `session_manager.py` is a solid Gemini-Live state machine.
- **`docs/OPERATIONS.md` / `SAFETY.md` are accurate** and trace cleanly to the code; `python main.py` run command verified correct.
- **No `pickle`/`yaml.load`/`verify=False`** anywhere.

## Prioritized remediation roadmap

**P0 — today (secrets):** Revoke & rotate all four keys. Remove literals from `config/api_keys.json`; load from env/OS keyring. Add `config/api_keys.example.json` with placeholders. Confirm nothing sensitive is in the OneDrive-synced copy.

**P0 — safety hardening:** (a) Stop `exec()`-ing model output, or strip `ctypes`/`pyautogui`/`shutil` from the namespace and route it through the `generated_code` confirmation class. (b) Close the default-LOW fall-through in `classify_action` so unclassified tools fail *closed*. (c) Unify to one shared `PendingConfirmationStore`. (d) Fix taskkill: drop `shell=True`, pass argv list, validate `^[A-Za-z0-9._-]+\.exe$`.

**P1 — restore test signal:** Rename `test_verify_changes.py` → `verify_changes.py` (or guard its `sys.exit`), set `testpaths = tools`, convert `test_memory_safety.py` checks to `assert`. Without this, every other fix ships unverified.

**P2 — root-cause architecture:** Collapse the four tool sources into one declarative `TOOL_REGISTRY` that generates the LLM schema and is the sole dispatch path for both engines (then delete the validator). Decompose `EraLive` into `AudioSession` / `WakeWordListener` / `ToolDispatcher` / `PromptBuilder`.

**P3 — docs & hygiene:** Document the vision/Ollama keys, fix the `jarvis_doctor` reference, reconcile the three product names, pin dependencies and align the Windows requirements file, and move `.checkpoints/` out of the source tree.

## Caveats

Static analysis only — nothing was executed except a scoped `pytest --collect-only` (`pytest tools` = 1692 tests collect clean; bare `pytest` crashes). No coverage tool is configured, so coverage gaps are inferred from file-to-module mapping, not measured. `main.py` is large; ~50 tool handlers were sampled, not exhaustively read — the systemic risk to review is the default-LOW fall-through (finding #3), which could hide other unconfirmed tools.
