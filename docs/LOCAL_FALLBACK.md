# Era — local fallback (24/7 without Gemini)

Branch `migration-voice-v2`, 2026-09-23. Baseline before the change: commit on `main`.

## What changed

Before: Era's hearing, thinking and speaking all ran inside one **Gemini Live** session.
When every Gemini key ran out of quota she slept **15 min – 2 h, deaf** (no mic, no wake
word), and typed/Telegram orders got *"my session isn't up"*. With the network down she
fell into a standby that could not hear the wake word either.

Now those outages are served locally:

```
                      Gemini Live up?  ── yes ──►  unchanged (Gemini voice loop)
                            │ no (quota on every key / bad key / rejected config / no network)
                            ▼
 mic 16 kHz ─► Silero VAD ─► Whisper-small (FR/EN, auto-detect)      core/local_stt.py
            ─► input safety gate ─► LocalBrain (Ollama, era.local)   core/local_brain.py
                                     │ tool calls
                                     ▼
                     EraLive._execute_tool  ← the SAME gate Gemini uses
                     (routing → classify_action → confirmation → execution → memory)
            ─► Fish Audio (if reachable) or Piper (offline)          core/local_tts.py
            ─► speaker
 Gemini re-tried every 5 min (quota) / 60 s (network); Era says so when she's back.
```

Also:

- **Text calls** (`generate_content` from ~20 tools): Gemini → spare key → Claude → **local
  Ollama** (new step 4). With the network down they go straight to local.
  `core/llm/quota_failover.py`.
- **Typed and Telegram** orders during an outage are answered by the local brain.
- **Streaming**: the local brain speaks sentence by sentence while it is still generating.
- **Barge-in**: talking over Era stops her (same detector as the Gemini path); "stop" /
  "arrête" cancels; "reconnect" makes her retry Gemini at once.

## Safety (security review, 2026-09-23)

The local path enforces the same gates as the Gemini path, plus a few of its own
because a small local model is easier to steer:

- **Voice lock:** every utterance is checked against your enrolled voice *before* it
  is transcribed (fail-closed). Standby wake uses the wake threshold.
- **Confirmations:** the model can never approve its own risky action. A
  ConfirmationRequired result ends the turn with the question. A confirm is accepted only
  as the first and only action of a new turn, and only if your latest words were a "yes"
  said after the action was armed.
- **Quoted JSON is never executed.** Text tool calls run only when the whole reply is the
  call, and never after a tool result has been read back.
- **Abort / reboot / mic-off / "stop" / "cancel"** are handled from the transcript
  without the model. "Stop" also drops any armed confirmation. The abort latch blocks
  local tool calls.
- **Pasted text / links / paths** from the keyboard or Telegram are framed as data.
- **History** keeps only a success/fail status for past tool results, not their content.
- Era's own speech is played one clip at a time, so she can't hear herself.

## Files

| File | Role |
|---|---|
| `app/era_live/local_fallback.py` | The loop, wired into `main.py`'s quota / auth / config / network branches |
| `app/era_live/local_guards.py` | Voice-lock and user-approval checks for the local path |
| `core/local_brain.py` | Streaming, tool-using conversation; fake-"Done" guard |
| `core/local_tool_calls.py` | Which model tool calls may run (text-call parsing, self-approval refusal) |
| `core/llm/adapters/ollama_chat.py` | Ollama `/api/chat` provider (streaming + tool calls) |
| `core/local_stt.py` / `core/local_tts.py` | Offline ears and voice (sherpa-onnx, CPU) |
| `config/llm.yaml` | `era.local` alias, `routing.local_fallback`, `quota_failover.local_alias` |
| `tools/local_voice_bench.py` | Latency benchmark |

## Install (already done on this PC)

1. `pip install sherpa-onnx` (was already installed).
2. Ollama running, with the local model pulled: `ollama pull gemma4:e4b`.
3. Models in `models/local_voice/` (git-ignored, ~515 MB), from the official
   sherpa-onnx releases (k2-fsa/sherpa-onnx) and its Hugging Face mirror:
   - `silero_vad.onnx`
   - `whisper-small/small-{encoder,decoder}.int8.onnx`, `small-tokens.txt`
   - `tts/vits-piper-fr_FR-siwis-medium/`, `tts/vits-piper-en_US-lessac-medium/`

If anything is missing, Era logs `[LOCAL] Fallback unavailable: <reason>` and behaves
exactly as before (waits for Gemini). Nothing crashes.

## Settings

| Setting | Where | Default |
|---|---|---|
| Kill switch for the voice loop | env `ERA_LOCAL_FALLBACK=0` or `routing.local_fallback.enabled` | on |
| Kill switch for text failover | env `ERA_QUOTA_FAILOVER=0` | on |
| Local model | `aliases.era.local.model` in `config/llm.yaml` | `gemma4:e4b` |
| How often Gemini is re-tried while local | `routing.local_fallback.probe_gemini_every_s` | 300 s |
| Tools offered to the local model | `routing.local_fallback.tools` | true (all 51, gated) |
| Whisper CPU threads | env `ERA_LOCAL_STT_THREADS` | 4 |
| Ollama address | env `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |

## Benchmark (RTX 3070 Laptop, 16 GB RAM, `python -m tools.local_voice_bench`)

| Stage | p50 | Target |
|---|---|---|
| STT (speech end → text) | ~1.1 s | 0.5 s — **over**: Whisper-small always encodes a 30 s window on CPU; threads don't help |
| LLM (text → first sentence) | 0.8–1.2 s | 1 s |
| TTS (sentence → audio) | ~0.2 s | 0.5 s |
| **Total** (+ ~0.5 s end-of-speech detection) | **~2.2–2.7 s** | 2 s |

Cold start: ~6–20 s the first time the local model loads its tool list — Era pre-warms it
while announcing the switch.

**Honest quality note.** The local brain is a *degraded mode*, not a Gemini replacement.
On the benchmark prompts `gemma4:e4b` performs routine actions (music, volume, apps,
reminders) most of the time but not always, sometimes answers French in English, and can
still guess live facts it cannot know. `qwen2.5-coder:7b` was faster but unusable for
actions (it faked "Done, sir."). The fake-"Done" guard means a failed action is admitted,
not claimed.

## Rollback

- **Instant, no code change:** set `ERA_LOCAL_FALLBACK=0` (voice loop) and/or remove
  `local_alias` from `routing.quota_failover` in `config/llm.yaml`, then restart Era.
- **Full:** `git checkout main` (the pre-migration baseline) and restart Era. The downloaded
  models can stay; nothing reads them on `main`.
