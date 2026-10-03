# EraOne stability update

Start here for this ZIP. This is the existing Windows assistant with reliability
repairs, its rebuilt HUD, regression tests, and a regenerated codebase manifest.
See `STABILITY_AUDIT.md` for findings, validation, and remaining limits.

This ZIP also includes the implemented Autopilot routing. Read
`AUTOPILOT_ARCHITECTURE.md` for its three lanes, supported commands, Claude setup,
fallbacks, and the cloud-voice boundary. The new routing is enabled by default;
`ERA_AUTOPILOT=0` disables it. It retains the previous stability repairs.

This release also adds the live visual workspace, direct task controls and staged
autonomous feature development with final HUD approval. Start with
`VISUAL_ENGINE_ARCHITECTURE.md` and `VISUAL_DEVELOPMENT_VALIDATION.md`.
`VOICE_24_7_PROPOSAL.md` describes the recommended voice continuity upgrade for
your RTX 3070 laptop; it is a proposal, not a newly activated provider chain.

## Install on Windows

Use **Python 3.12, 64-bit**. Extract to a fresh folder. Keep your existing API
configuration, models, voice enrollment, and memory backed up before replacing
an existing installation. This ZIP does not supply your credentials or models.

From PowerShell in the extracted folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

The script creates `.venv`, installs the Windows dependencies with the tested
core constraints, installs Playwright Chromium, generates the manifest, and
creates `config/api_keys.json` from the example only if it does not exist.

Configure a real Gemini key in `config/api_keys.json`, or set it for this terminal:

```powershell
$env:GEMINI_API_KEY = "your-real-key"
.\.venv\Scripts\python.exe tools\era_doctor.py
.\start_windows.cmd
```

`ERA_LIVE_MODEL` can override the Live model. Otherwise the model comes from
`aliases.era.voice.model` in `config/llm.yaml`, re-read at connection time.
Use a model available to your account that supports Live audio and tools.

## If typed commands work but voice does not

Voice verification defaults to ON. Without a compatible speaker model and an
enrolled voice, it deliberately blocks spoken input. Startup and Doctor now
explain this instead of leaving it looking like a silent response failure.

Install the optional voice engine, set the speaker model path, then enroll:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.local.txt -c constraints.stability.txt
$env:ERA_SPEAKER_MODEL = "D:\your-model-folder\nemo_en_titanet_large.onnx"
.\.venv\Scripts\python.exe tools\enroll_speaker.py --check
.\.venv\Scripts\python.exe tools\enroll_speaker.py
```

The path must point to an actual compatible model; the ZIP has no model weights.
Read `docs/speaker_recognition_integration.md` for calibration and enrollment.
Vosk wake detection also needs its model; `config/wake_word.json` controls the
actual wake word. A missing microphone now retries locally while typed commands
and the cloud session remain available.

## Offline fallback and other integrations

Installing `sherpa-onnx` alone does not enable offline voice. The STT/TTS model
files and the configured Ollama model must also be present. Follow
`docs/LOCAL_FALLBACK.md` and check the `era.local` alias in `config/llm.yaml`.
The CodeCraft cascade additionally needs its own endpoint, model, and key.

Claude profiles in the supplied registry refer to the original machine.
Configure the paths/accounts on your PC before expecting Claude fallback to work.
Telegram, Spotify, Gmail, advertising/publishing, and other integrations retain
their existing credential and application requirements.

## Verify and troubleshoot

```powershell
.\.venv\Scripts\python.exe -m pytest tools\test_stability_regressions.py -q
.\.venv\Scripts\python.exe tools\era_manifest.py --check
.\.venv\Scripts\python.exe tools\era_doctor.py
```

Run the full suite with `python -m pytest` from the activated environment. Review
`logs/era.log` for `engine.command.failed`, reconnect, and audio-device records.
The regression suite uses fake transports/devices; it does not contact a real
model or operate your desktop.

On the target PC, verify a typed question, a spoken question, a mic unplug/replug,
a model/voice switch, a network interruption, and a clean shutdown. These live
checks are needed before calling the installation fully operational.
