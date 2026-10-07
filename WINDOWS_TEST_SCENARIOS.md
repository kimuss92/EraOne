# Era One — Windows acceptance tests

Use this checklist on a **copy** of your existing installation first. Keep your working installation, credentials and memory as a backup. This release contains the uploaded memory data; do not overwrite newer memory on your machine with an older copy from this ZIP.

## Before starting

1. Extract into a normal writable folder, outside `Program Files`. Keep the full folder structure.
2. Open PowerShell in that folder. Run `powershell -ExecutionPolicy Bypass -File setup_windows.ps1` and review its output. For an existing environment, install `requirements.windows.txt` into the Python environment that starts Era. The bundle is already rebuilt; Node LTS/npm are needed when Era generates future React changes.
3. Run `claude --version`, then `claude` and complete sign-in if needed. Era's coding lane uses tool-less Claude Sonnet through `era.reason`. Existing named profile configuration is preserved in `config/llm.yaml`. If those profiles are not yours, set `$env:ERA_CLAUDE_ACCOUNTS = "$env:USERPROFILE\.claude"` in the shell that starts Era. `CLAUDE_BIN` can point to the actual Claude executable/shim. These environment settings must be set before launching Era.
4. Keep your existing `config/api_keys.json` and other credentials locally. They are not included in the delivered archive. Configure your own keys using the example configuration if this is a fresh install.
5. Run `python tools/era_doctor.py`. Resolve required dependency/configuration findings before testing. A passing candidate test suite does not mean missing credentials/devices are ready.
6. Start using `start_windows.cmd`. Open Development → describe “Improve interface activity animation” → Find source. It should list actual React/Python files and report installed toolchain components, without requesting a filename or using an LLM.

## Scenarios

Run in order. Keep each request small so it is clear what was verified.

| # | Request/action | Expected result |
|---|---|---|
| 1 | Inspect the HUD while idle. | No “LIVE WORKSPACE” toolbar or extra Tasks button above the centre. Scheduled tasks stay in Scheduled / its drawer. Development sits immediately left of the clock. No invented CPU/RAM values when unavailable. |
| 2 | Ask “Set system volume to 30 percent”, then “Get system volume”. | The lane router routes the exact deterministic command locally; the second result verifies the first. Live Activity records the actual result. System and glasses volume remain distinct. |
| 3 | In Development enter “Improve persistent memory recall” and click Find source. | Relevant memory sources and dependencies appear. Protected framework files are marked review-only. No generation, billing or source edits from this inspection. |
| 4 | Ask “Add a local word counter feature. Accept a text string and return its word count. Handle empty text and repeated whitespace. Include tests and no network calls.” | A staged Claude candidate creates `features/<name>.py` and a focused test. It plans its invocation through `run_feature`; no code_helper on own files. Task name and liquid progress appear in the LEFT activity panel. |
| 5 | During that job inspect Live Activity, then Development. | Percentages change only at real verification checkpoints. They may stay still while a long step runs. Detailed phase/check information is in Development. An unmeasured ordinary tool uses “Working”, not an estimated numeric percentage. |
| 6 | When it reaches awaiting approval, inspect files, diffs, tests and diagnosis. Try the approval button before checking the review box. | Activation is disabled before review. The candidate is not claimed live. Existing live files are unchanged. Diagnosis findings remain visible even when code tests passed. |
| 7 | Check the review box and approve. Ask “List my activated development features”, then run that exact feature ID on “Era is ready now”. | The feature catalog includes the new ID. `run_feature` requests confirmation; after approval its result is 4. Standalone feature modules activate live. Ordinary engine Python changes explicitly require a restart. |
| 8 | Restart Era and list/run the same activated feature again. | Approved deployment history restores the feature only if its file hash still matches the approved revision. It remains callable after restart. |
| 9 | Ask “Remember that my test project is named Era acceptance”, close and restart Era, then ask for the name. | Explicitly authorized fact is persisted and recalled. Search it in Memory. An unrelated sentence must not silently become a permanent user fact. |
| 10 | Ask “Add a RAM usage metric to the HUD”. Then ask to change the HUD accent to `#38bdf8`. | Supported declarative visuals update without relaunch. Metric values bind to backend readings; the assistant does not invent values. There is still no empty workspace toolbar. |
| 11 | Have Era read a harmless `.txt`/`.md` file or list a folder. Use Context → Open / Show in folder and Sources. | Compact controls match the theme. Open opens the known document/folder; Show in folder reveals it. Unknown paths and executable files keep the existing restrictions. |
| 12 | Create a reminder “Era acceptance reminder” for two minutes from now in Scheduled. Pause it, resume it, then cancel it with confirmation. | The real stored task changes. It does not execute after cancellation. Stale task versions are refused rather than applying an outdated UI action. |
| 13 | Stage another small feature, then Reject its verified candidate. | State becomes rejected; no live code or feature registry changes. Rejection persists across restart. |
| 14 | On the TEST copy only: stop Era; temporarily set `CLAUDE_BIN` to a nonexistent path in the launching shell; restart and stage a small feature. Restore/unset it afterward and restart. | A visible missing-CLI failure with an actionable next step, no live source change and no hidden Gemini code generator. After fixing the environment, Retry creates a new job and still requires fresh final approval. |
| 15 | On the TEST copy, start development then exit Era during planning/verifying. Restart. | Interrupted job becomes failed with a restart explanation, rather than appearing permanently busy. Live source was not activated. A new job can start. |
| 16 | Resize the HUD, toggle both rails, and enable OS reduced-motion preference. | No header overlap on narrow screens. Development remains reachable. Liquid animation becomes static with reduced motion; progress/state still update. |

## Repair and failure tests

Automated tests cover a failing candidate, one evidence-driven repair, a second failure stopping the loop, malformed JSON correction, missing/authenticated-provider errors, approval hash tampering, stale source, interrupted deployment rollback, damaged jobs, damaged newest memory backup with recovery from an older valid copy, and refusing to overwrite unrecoverable memory.

Run these offline checks in your installed Python environment:

```powershell
python -m pytest -q tools/test_autonomy_recovery.py tools/test_development.py tools/test_visual_engine.py tools/test_autopilot.py tools/test_task_scheduler_authority.py tools/test_self_improve_locator.py tools/test_safe_store_replace.py --timeout=40
```

For the full copied-worker exercise (uses authored candidates, no Claude request):

```powershell
python tools/verify_autonomous_development.py --frontend
```

This can take several minutes and needs npm plus the installed project/test dependencies. It uses temporary source copies, requires an explicit activation in its test harness, and checks that a failing candidate cannot land.

## Send back a useful failure report

Send the exact request, expected vs actual result, a screenshot of Development/Live Activity, the job ID, and the relevant verification/doctor output. Development records and logs are under `runtime/development/<job-id>/`. Remove API keys, credentials and personal content before sharing logs. Do not send secrets.

No zero-failure guarantee: cloud access, Windows integration and arbitrary new code still require real-machine testing. Recovery is deliberately bounded; side-effecting failed tools are not automatically replayed. Final approval remains required for source activation.
