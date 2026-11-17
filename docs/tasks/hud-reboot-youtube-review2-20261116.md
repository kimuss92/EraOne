# PF-036/037 corrected candidate for independent re-review

Please review the exact current bytes pinned below against Claude's findings F1 and F2 in `docs/tasks/hud-reboot-youtube-claude-review-20261116.md`. The original scope and unchanged PF-035 candidate remain in `docs/tasks/hud-reboot-youtube-review-20261116.md`. The worktree has other agents' pre-existing edits; only the functions and tests named here are this correction.

## F1 — muted owner reboot

`app/era_live/tool_dispatch.py::_arm_relaunch` now records the HUD mute choice in the one-shot marker. `core/era_relaunch.py::consume_ready_ack` returns a truthy `"muted"` result when a fresh successor process consumes a muted marker. `main.py` treats that result as a completed requested reboot, suppresses both the requested-reboot acknowledgment and ordinary first-boot greeting, and consumes the marker once. An unmuted marker retains the previous audible behavior. `tools/test_era_relaunch.py` asserts the muted marker, its one-shot consume result, and the readiness speech condition.

## F2 — Latin-only transliteration

`utils/media_match.py::title_spelling_variant` keeps the identical six-plus consonant skeleton requirement but accepts a 0.7 character ratio, allowing the logged `Sabr Al Rijal` versus Latin-only `Saber Elrjel` title. The variant can raise only a weak score to 0.86; exact scores and distinct-recording ambiguity remain as before. Its docstring now accurately says Arabic is approximately transliterated. `tools/test_media_hud_reliability.py` covers both the logged Latin-only title and a full-version title, plus a same-consonant-count wrong title that must not match.

## Pinned bytes (SHA-256)

```text
core/era_relaunch.py                 1083d28c1ec883ed476873ec3f88e5ee08e8a7dcb9af4563f7fc102a6f121989
app/era_live/tool_dispatch.py        bf942ab86b8f6648a2bb96be37c2fbb723146b5f875a102cb6684aebe1c6b511
main.py                              2748d2c9c9e84f57e8e444df34a70dcd09ec397ca55d654fffc9665ea22b1c55
utils/media_match.py                339887b0bb10988873a9d10410ecad5384b79da6d51b71121af1c3aeab3b171b
tools/test_era_relaunch.py          c8e0c4dbe88d7c26b54b72445cf2ab849eae5171a426573b363a43947424c016
tools/test_media_hud_reliability.py 0f68d5cf8cb7d92e87d2d551a9318625486b6d373a5627501089f419679ab34d
docs/PROTECTED_FIXES.md             3c58ebc772ebf7394fc8fef3fbdb99166b2d16f2be0df151358dd31696ea8ed3
```

Windows checks on this correction: 233 focused tests passed, 2 dependency warnings; all 409 protected guards passed, 2 dependency warnings; Ruff lint and format clean; protected registry OK. No live restart or media was run. Acceptance is pending your independent re-review of these bytes.
