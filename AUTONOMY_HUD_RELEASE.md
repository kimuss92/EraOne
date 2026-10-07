# Autonomous development and HUD update

## Changes

- Restored the missing `description_tokens` function used by the source locator, wired read-only `find`/`locate` into dispatch, confirmation and scheduled-action exemptions, and included subsystem routing data in the generated manifest. The uploaded archive had mismatched locator code, tooling and metadata.
- Added a bounded local source map for Python and React: filename, symbols, descriptions, direct Python dependencies, applicable skill excerpts, editable/review-only status and installed toolchain checks. It excludes state, dependency directories and symlinks outside the project. File size/mtime invalidates cached entries.
- Development now includes source/wiring context and related persisted outcomes in Claude Sonnet's prompt. New `features/*.py` modules register through the existing approved `run_feature` contract. Framework/safety/declaration changes remain protected; they need a separately reviewed patch.
- Added at most two candidate attempts. The second receives actual verification evidence and the previous candidate. Invalid model JSON gets one schema correction. Missing CLI/auth/budget failures stop with concrete next steps. Failed side-effecting tools are not replayed automatically.
- Existing named Claude profile failover remains in place. No hidden Gemini code-generation fallback is introduced in staged development.
- Added atomic, flushed development journaling; checkpoint progress from the real worker; interrupted repair recovery; explicit recovery-required reporting if rollback cannot safely complete; and Retry from current source with fresh verification/approval. History survives process restart in job records and is kept separate from personal memory.
- Persistent memory writes flush before atomic replacement, search older backups if the newest is corrupt, and refuse to replace unrecoverable stored facts with empty memory. The uploaded memory files are retained in the archive; preserve newer local data during installation.
- Migrated active text callers/defaults to documented stable Gemini `gemini-3.8-flash` and the lightweight `gemini-3.1-flash-lite`. Added `ERA_GEMINI_SMART_MODEL` / `ERA_GEMINI_FAST_MODEL` overrides for the central defaults. Vision's new model uses a supported thinking level. Historical fixtures, image-generation IDs and the Live voice configuration are separate model contracts.
- Removed the empty centre Live Workspace toolbar and Tasks shortcut. Kept optional declarative visuals. Development is beside the clock; its drawer has source inspection, job history, readable checks, diffs, final review and retry.
- Added a compact liquid progress bar to LEFT Live Activity with only the task name and percentage. Tool progress without measured checkpoints is indeterminate. Offline status freezes the last progress display. Reduced motion disables the animation.
- Refined file/folder controls in Context/Sources and hardened incomplete WebSocket snapshots so a missing transcript no longer blanks the HUD. Fixed narrow-screen header overlap.

## Verification boundaries

Read `validation/autonomy-validation.md` and `WINDOWS_TEST_SCENARIOS.md`. Code, UI and staging checks have been run here. This environment has no authenticated Claude or real Windows audio/device/session, so actual Claude generation, Windows actions, startup and personal-memory recall must be confirmed on your machine. Tests do not prove every third-party integration or arbitrary generated feature works.

Generated code runs locally after approval. The verifier uses a separate source copy/process, **not** an OS sandbox. Engine Python changes require restart; standalone feature modules and approved frontend bundles can reload live. Staged diagnosis may flag missing secrets/devices and is reported separately from deterministic code gates.

## Model documentation used

- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite

The named model is documented as available; entitlement/quota for your actual API project still needs a live request. This release does not implement the deferred 24/7 voice-provider proposal.
