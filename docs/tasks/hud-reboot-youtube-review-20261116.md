# GPT implementation candidate — HUD inactivity, reboot acknowledgment, YouTube selection

Owner assignment: `qitem-20261116192003-c298d30a84770869` (`dev-review@era-one` builds; `dev-build@era-one` independently verifies). Repository candidate is the current working-tree bytes at the paths below. The repository has a broad pre-existing dirty baseline, so the full `HEAD` diff is not an honest representation of this assignment. Reviewer: inspect only the listed hunks/functions in the current files and use the SHA-256 values to pin the candidate. Do not treat other hunks in those files as this assignment.

## Scope 1 — PF-035 themed inactivity and entry cue

- `ui_src/src/components/CoreStage.tsx`: `.era-inactivity` semantic status markup, wake hint, online/inactive condition.
- `ui_src/src/eclipse.css`: existing theme warning, panel, text and accent tokens; opaque `--panel-solid` surface; no animation.
- `ui_src/src/lib/transport.ts`: cue only on a live active-to-inactive listening-mode transition, not the initial snapshot/reconnect.
- `ui_src/src/lib/sounds.ts`: short two-tone cue routed through existing enable/unlock/visibility/speaking gates, with microphone capture bypass only while backend listening mode is inactive.
- Guards: `tools/test_hud_inactivity.py::test_inactivity_cue_requires_a_live_entry_transition_only`, `::test_inactivity_indicator_uses_theme_tokens_and_static_accessible_markup`.

Visual evidence: isolated Vite preview, disconnected backend, synthetic badge only. Light/Aurora screenshot: `C:\Users\nesri\AppData\Local\Temp\era-inactivity-preview-light-aurora.png`; dark/Neon screenshot: `C:\Users\nesri\AppData\Local\Temp\era-inactivity-preview-dark.png`. This checks rendered theme contrast and placement, not the live backend transition or sound unlock path.

## Scope 2 — PF-036 requested-restart readiness acknowledgment

- `core/era_relaunch.py`: expiring, one-shot, owner-source readiness marker; same-process PID cannot consume; stale/invalid markers discarded.
- `app/era_live/tool_dispatch.py`: pre-exit spoken notice on the authorized owner reboot path, except when muted; arm marker only for a recoverable requested relaunch.
- `main.py`: consume marker once only after the new connection is ready; suppress on muted/standby and avoid ordinary boot/reconnect greeting duplication.
- Guards: `tools/test_era_relaunch.py` readiness marker TTL/source/PID/one-shot, notice and startup ordering cases.

No live restart was performed. Deterministic Windows tests cover normal consume and marker failure/expiry, but an interrupted real relaunch and live voice timing remain owed.

## Scope 3 — PF-037 multilingual YouTube match and useful ambiguity

- Root evidence is in `logs/eraone.log` around 20:17:17–20:17:35 and the owner screenshots in the assignment: the `youtube_video` route navigates/searches, extracts three cards, then raises `VideoSelectionRequired` for both queries. The observed router `target=spotify` is diagnostic context, not the failing dispatch. Screenshot results include a transliterated Mikasa title and a distinct longer/full recording.
- `utils/media_match.py`: bounded Latin/Arabic approximate transliteration spans with consonant and similarity constraints; only rescues weak matches.
- `actions/youtube_video.py`: ambiguous response includes up to five visible titles; clear match keeps exact watch ID and advancing-playback verification.
- `tools/test_media_hud_reliability.py`, `tools/test_media_selection.py`, `tools/test_youtube_direct_play.py`: realistic transliteration, distinct recording ambiguity, useful titles and no playback on ambiguity.

No live media was started. Exact candidate identity and actual playback remain live checks owed.

## Candidate pin

SHA-256 of the exact current source/artifact bytes:

```text
ui_src/src/components/CoreStage.tsx  7dcb14009d27a24614273bbbf80f07a6f1c93f82b211e416a7b32b38d726ead0
ui_src/src/eclipse.css                fa3c838cf55a4f7b2ff9e63f97ed324ea0f0bdc5df55de6054ef6e4f40acc75c
ui_src/src/lib/transport.ts           309d06d0d8ae05099e206fee7de465d487fb8ccc42b41576174d3a6d2a0adb31
ui_src/src/lib/sounds.ts              b4f7054d0d0fd5a4aebfba2d722fde8331eac1d2ef0ad57e7be24aca30785625
core/era_relaunch.py                  2b648c85cd2852bee9926558a11845d70a7d7897580bda3d3c21e1163daa6d68
app/era_live/tool_dispatch.py         a22f5424f8b8f2c5ad1b74edc9da4bf1d8832c094fd8e74a108e6db6d1d83af3
main.py                               a61d274aeb3a6b8c74f218ee4b6d515868f5cce51b3856af81797746ed0c51e2
utils/media_match.py                 f0af945e06537a99dd8de7d384e0b3f6929148ee2e99613234c8e28b6f335b29
actions/youtube_video.py             eff00af0604ef69390a68bef0eabc1945b8c390ea67f8b80db352b3eefcadf38
tools/test_hud_inactivity.py         dca62f1a2f377ff7b9af5322bd794c34369597b3c893fb9e35e1e6d38eac0c48
tools/test_era_relaunch.py           90a9add82ce241074c9a91751b68760bc095e043fb41bc5ba2366825ff6b9cbf
tools/test_media_hud_reliability.py  ba5cf86b7a145dec0336efa665797823cceae07852f50e34e031b4553d4c47f1
tools/test_media_selection.py       fe91406aa7952cecba35f6cfd2d874b111127792e50c1b1eafc273ee9504430c
tools/test_youtube_direct_play.py    d1236bbd2d64d2a5e390e70a8f05dc4acf1a6d0eb80a5b5579a04f226e7d60fb
docs/PROTECTED_FIXES.md              7a37107647f44f26681525804017e38a5089bd139db0306943978bdedd3e5933
ui/dist/index.html                   f7055bfecf698518cc82edc50d345474c76f86315119aa7c285d95ce865056d0
```

## Verification reported by builder

- Windows focused regression run: 4,219 passed, 2 warnings.
- Windows protected guards: 409 passed, 2 warnings; registry check: REGISTRY OK.
- Windows Ruff check and format check passed before the final CSS-only solid-surface adjustment.
- Windows production Vite build passed after that adjustment (1,844 modules; `ui/dist/index.html` 741.73 kB).
- No live restart, microphone, YouTube playback, or listening transition was exercised.

Independent review and disposition are pending. Do not mark this candidate accepted until dev-build records a verdict against these pinned bytes.
