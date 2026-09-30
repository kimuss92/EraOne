# Fable Handoff — 2026-07-06

## 1) Goal
Improve Era without breaking it: (a) new **design_studio** tool — user describes a kitchen/wardrobe/any design in natural language (layout L/I/U-shape, textures, decoration) → Era builds a pro prompt (Gemini) → generates image or video; (b) new **self_diagnosis** tool — Era deep-scans her own code and proposes fixes; (c) fix/harden **premiere_control**; (d) speed pass; (e) UI untouched by design (compiled React bundle — resource-safe call).

## 2) Current state
- All features implemented and wired (declarations + dispatch + prompt.txt routing + safety LOW_TOOLS). Not yet live-tested in a running Era session.
- Ruff clean on all new/changed files. `tools/` suite: **1683 passed, 9 failed — all pre-existing** Netflix-control drift (never touched this session).
- Root suites: test_hermes_router, test_dispatch, test_task_queue, test_pending_ask_suppression, test_caption_footer pass. **test_hermes_wiring (3) + test_memory_guard (3) fail — pre-existing stale stubs**: their `_Stub` classes lack `safety_gate` and `_cm_handoff_active`, attributes main.py gained in earlier sessions (`main.py:1706`, `main.py:2725`). Fix is in the tests, not main.py.

## 3) Active files
- `actions/design_studio.py` (new) — NL → pro prompt (applies delta_cuisine design_rules from brands.yaml for kitchen/wardrobe) → image (`gemini-2.5-flash-image`) → optional video (Veo `veo-3.0-fast-generate-001`, fallback veo-2). Output: `runtime/designs/`, auto-opened.
- `actions/self_diagnosis.py` (new) — py_compile sweep + era_doctor subprocess + era.log tail scan + Gemini-ranked fixes. Report: `runtime/diagnosis/diag_*.md`.
- `actions/premiere_control.py` (rewritten) — config-driven paths/model (`premiere_mcp_server`, `premiere_temp_dir`, `premiere_model`, `premiere_timeout_s` in config/api_keys.json), bounded tool loop (8), history trim at user boundaries, 180s hard timeout, server-script existence check.
- `main.py` — imports (~line 244), two TOOL_DECLARATIONS entries (end of list, ~line 1282), two background dispatch handlers next to premiere_control's (~line 3140).
- `core/safety_guard.py` — design_studio + self_diagnosis added to LOW_TOOLS (~line 327). *security-reviewer pass on this not yet run (project convention).* 
- `core/prompt.txt` — routing-table rows + DESIGN STUDIO RULE + SELF-DIAGNOSIS RULE (~line 142).
- `core/ai_provider.py` — cached `get_client()` (one genai.Client per key, was per-call).
- `era_dispatch.py` — `load_brands()` mtime-cached.
- `tools/test_first_patch_smoke.py` — stale One Piece test updated to match newer contract in test_browser_media_routing.py.

## 4) Changes made
See §3. Nothing removed; reconnect/GoAway logic, safety-gate semantics, memory layer untouched. Video path degrades gracefully (no Veo entitlement → keeps image + pro prompt). Both new tools run backgrounded (`asyncio.to_thread`) and speak on completion.

## 5) Failed attempts
- None substantive. One Edit failed on whitespace mismatch (re-applied). Full-repo `ruff check` shows pre-existing noise (E402 in main.py, F541 in safety_guard, B904 in era_dispatch) — deliberately NOT mass-fixed to keep the diff safe.
- Last command (re-running tools/test_netflix_scan_js.py for triage) was interrupted by user — Netflix failure triage never started.

## 6) Next steps
1. Fix stale stubs: add `safety_gate` (with `classify_typed_input`) and `_cm_handoff_active` to `_Stub` in test_hermes_wiring.py and test_memory_guard.py.
2. Triage the 9 pre-existing Netflix test failures (test_netflix_play/scan_js/search_robust/simple_mode/vision_fallback) — code-vs-stale-test drift.
3. Restart Era; live-test: "show me an L-shaped kitchen in matte white quartz…" (design_studio), "run a diagnosis on yourself" (self_diagnosis). Confirm Veo quota/entitlement on the Gemini key.
4. Run the `security-reviewer` subagent over the `core/safety_guard.py` LOW_TOOLS addition (project convention after touching safety layer).
5. Optional: tune image/video model names via config if models rotate.
