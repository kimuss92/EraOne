# Stability update validation

Audit environment: Linux, Python 3.12.14; Windows is the target platform.
No real Gemini credential, microphone, Windows desktop, or Claude CLI was used.

## Results

| Check | Result |
| --- | --- |
| Original broad baseline | 5,175 passed, 78 failed, 38 skipped |
| Broad run during repairs | 5,271 passed, 43 failed, 27 skipped |
| Final focused runtime, audio, safety, file, UI, and manifest suite | 668 passed, 1 skipped |
| Final Hermes routing/execution diagnostic recheck | 21 passed, 1 skipped |
| New failure regression tests (included in focused suite) | 33 passed |
| Ruff repository check | Passed |
| HUD production build (`npm ci --ignore-scripts`, `npm run build`) | Passed |
| HUD TypeScript (`npx tsc --noEmit`) | Passed |
| Manifest generation | 291 modules, 53 tools, zero unparseable modules |

The broad run started before the last protected-path fix and test-mock update.
Its two scheduler failures and execution-layer diagnostic are covered by the
final passing checks. Three Hermes tests mocked the scheduling function to
return `None`; the mock now returns the actual task, matching the future-returning
API and allowing the error observer to attach its callback. Their assertions
are unchanged and pass. The full suite was not repeated after these final edits.

The remaining broad failures cover Windows path/Explorer/registry/COM semantics,
unavailable pyautogui/desktop capture/Claude CLI, and existing file-candidate
score/message assertions. These features need target-PC validation. The broad
suite is not fully green, and this update does not certify every integration.
See `validation/broad-suite.txt` for the complete failure list and traces.

## Test environment details

PortAudio and Qt system libraries were staged outside the source tree so real
Python imports could run in this container. Qt used `QT_QPA_PLATFORM=offscreen`.
A temporary external bootstrap pointed sounddevice at the staged PortAudio
library. This bootstrap and native libraries are not included in the ZIP.

The broad and final focused runs used the inert string `era-unit-test-key` as
`GEMINI_API_KEY` for tests that check key presence. It is not an authorized key
and does not demonstrate live cloud responses. Failure regressions use fake
transports/devices. Frontend dependency versions came from the existing lockfile.

## Doctor and live acceptance

Doctor reports **NOT READY: 4 errors and 7 warnings** in the supplied setup:
missing Gemini credentials, Cloudinary publishing configuration, Claude CLI,
and the original machine's Obsidian vault. It also warns that no voice is
enrolled and about optional packages/desktop imports. The report is included in
`validation/doctor.txt`; these requirements are not hidden by the repairs.

On Windows, follow `README_STABILITY.md`, then verify:

1. A typed reply and a spoken reply with your Gemini account/model.
2. Enrollment and the intended voice verification behavior.
3. Microphone unplug/replug while typed input continues.
4. Network interruption, reconnection, and a clean shutdown/restart.
5. Offline STT/model/TTS if configured, plus each integration you use.
6. A sustained session and the full test suite on the target installation.

Windows launch scripts were reviewed but cannot be executed on this Linux host.
Constraints record exercised core versions; they are not a complete Windows
lockfile. Model weights and account configuration are not included.
