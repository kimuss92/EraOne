# Era One: 24/7 voice continuity proposal

Proposal based on the supplied screenshots: RTX 3070 Laptop GPU (8 GB VRAM),
Core i7-12700H, 16 GB RAM. This document is a proposal, not an installed new voice
provider chain. The ZIP's implemented HUD/staging changes are described separately.
Provider documentation checked 2026-10-03.

## Recommendation

Keep the microphone, speech recognition, interruption detection and speaker
independent of Gemini. Use the lane router to handle exact local commands, then route
reasoning to a healthy streaming cloud provider or a warm local model. Keep one
chosen TTS voice across all reasoning providers.

| Profile | Recognition | Reasoning | Voice | Cost/continuity |
| --- | --- | --- | --- | --- |
| Free/offline | Local sherpa-onnx; suitable streaming recognizer where available, current Whisper as fallback | Small quantized model through Ollama plus no-LLM lane-router actions | One fixed local Piper voice for the chosen language | No API quota; electricity/hardware still cost money; weaker reasoning and less expressive voice |
| Premium practical | Local recognition | Streaming Gemini text or paid Groq, then local backup | Existing Fish Audio reference voice; local Piper when unavailable | Least integration work because Fish exists already; cloud failure changes timbre when Piper takes over |
| Premium voice alternative | Local recognition or paid streaming STT | Streaming cloud providers, then local backup | ElevenLabs Flash v2.5, fixed voice_id; local fallback | New adapter needed; low synthesis latency; metered credits, internet and provider limits remain |
| Strict same voice offline | Local recognition | Cloud/local reasoning can change independently | Always use the same local TTS model and voice | Best continuity of timbre during internet or provider failures |

The native Gemini voice in the current integration is produced inside Gemini
Live. It cannot remain available when that service's quota is exhausted. Fish or
ElevenLabs can keep the same selected voice across Gemini/Groq/Ollama *reasoning*
changes, but a switch to Piper during a TTS outage will sound different. Choose
local TTS all the time if an identical voice during outages is the highest priority.

## What is already present

- `core/local_stt.py`: local Silero VAD plus Whisper-small int8 through sherpa-onnx.
- `core/local_tts.py`: local English/French Piper voices; model files are required.
- `core/local_brain.py`: streaming Ollama replies, spoken sentence by sentence,
  with tool calls passed through the same safety gate as Gemini.
- `app/era_live/local_fallback.py`: serves local voice during quota/network outages.
- `core/tts_fish.py` / `core/voice_mode.py`: Fish reference voice plus Gemini audio.
- `core/llm/adapters/openai_compat.py`: streaming compatible text endpoints, currently
  configured through CODECRAFT_* settings. It can be evaluated against another
  compatible provider; compatibility/tool-call behaviour must be tested.
- `app/era_live/cascade.py`: standalone local-input/TTS pipeline. It is disabled in
  `config/llm.yaml` because the previously tested endpoint buffered replies.
- `era_supervisor.py` and `register_era_supervisor.ps1`: process recovery/startup.

Missing downloaded models, unenrolled speaker verification, unavailable Ollama,
or missing credentials can prevent fallback readiness. A ZIP containing the
Python code alone does not install those models. The existing Whisper path decodes
completed utterances; sentence streaming on output does not make its input a
streaming recognizer.

The repository records prior laptop measurements of about 1.4 seconds for local
recognition of a two-second utterance, and 2–7 seconds for warm local reasoning.
Those are source-recorded historical measurements, not benchmarks performed in
this audit. The current local fallback should not be described as Gemini-fast.

## Change the execution flow

1. Start the local microphone and interruption loop once. Do not tear it down
   when a cloud model/session is changed. Restore the chosen TTS preference on boot.
2. Use local wake-word/VAD and speaker verification. For lower latency, evaluate
   a streaming recognizer for the spoken language. Keep the existing Whisper path
   when accuracy/language coverage demands it.
3. The lane router dispatches precise volume/window/file/HUD commands with no model.
   Coding remains a separate Claude staging job so it never blocks the audio loop.
4. Conversation uses a bounded voice prompt and only relevant tools. Stream the
   first useful sentence to TTS; do not wait for the full response.
5. A model router uses health state, remaining budget and latency. Proposed order:
   preferred cloud text model -> independent paid streaming provider -> warm local.
   A free cloud allowance is an extra convenience, not the reliability floor.
6. A quota error opens that provider's circuit. Respect Retry-After; do not retry
   the exhausted service for every microphone turn. Probe recovery in the background.
   Authentication failures are not retried as if they were ordinary congestion.
7. Keep canonical conversation/tool history outside providers. Cancel old stream
   generations and audio on interruption; use turn IDs to discard late output.
   Switch automatically only before the first audible output or action. An action
   already sent must not be replayed through a second provider.
8. Show the active brain and voice in the HUD. Persist task state, approvals and
   budgets. Require real 24-hour soak testing before claiming continuous operation.

For your 8 GB GPU, start with a 4B-class quantized model and a short context. For
example, Ollama lists `qwen3:4b` at 2.5 GB of model files; runtime memory also needs
KV cache and working buffers. Benchmark it on Era's actual tool and confirmation
cases before changing `era.local`, currently `gemma4:e4b`. Model tags and installed
quantizations can change; pin the tested build. Avoid large local models or very
long contexts on this laptop, especially while other GPU apps are open.

Keep speech processing on CPU initially, one LLM instance warm, and leave memory
headroom. A future 32 GB RAM upgrade could reduce pressure during multitasking;
it is not required to start testing the proposed small-model setup.

## Free and premium facts

Google applies Gemini quotas per project, not per API key. Creating more keys in
one project does not increase quota. Paid API billing can raise limits, but limits
still exist. Session resumption and context compression address connection/session
lifetime, not quota exhaustion.

Groq publishes both free and Developer plan limits; organizations still have rate
limits. Its current production list includes `openai/gpt-oss-20b` at $0.075 per
million input tokens and $0.30 per million output tokens. These are text costs,
not all-in voice costs or a guaranteed response time. Validate its tool support
with Era and measure time to first *audible* reply from your network connection.

ElevenLabs documents Flash v2.5 synthesis latency around 75 ms, excluding application
and network latency. It is not a 75 ms full assistant turn. Current standard
monthly plans include Starter $6/30,000 credits and Creator $22/121,000 credits;
promotions and first-month discounts differ. Credits depend on product/model and
continuous speech will exceed small plans. Fish offers a free developer TTS model
and paid models, but availability, limits and your account's cost must be checked;
no unverified unlimited plan or Fish price is assumed here.

## Operational setup

Run Era in the logged-in Windows user session for GUI/microphone access. Use the
existing supervisor/startup registration, prevent sleep while plugged in, retain
crash logs and avoid starting multiple instances. A headless Windows service is
not a direct replacement for an interactive desktop agent. Keep the screen free
to turn off without putting the machine to sleep.

Your dated screenshot shows GPU 81°C and CPU 90°C; that is not a current reading.
For all-day operation, test sustained temperatures, clean airflow and use sensible
power settings rather than keeping every model at maximum load. Use a 24-hour soak
that includes Gemini 429 errors, internet loss, audio-device reconnect, simultaneous
coding jobs, interruption, restart recovery and budget exhaustion. Measure p50/p95
first-audible latency and memory growth; set latency targets after these tests.

Neither free cloud access nor a paid plan guarantees zero downtime. Local hearing,
local TTS and local reasoning provide the fallback floor while the laptop itself
has power, healthy devices and sufficient resources.

## Primary sources

- Gemini limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini session handling: https://ai.google.dev/gemini-api/docs/live-api/session-management
- sherpa-onnx local speech/platform support: https://github.com/k2-fsa/sherpa-onnx
- Ollama Qwen model sizes: https://ollama.com/library/qwen3
- Groq production models/prices: https://console.groq.com/docs/models
- Groq limits: https://console.groq.com/docs/rate-limits
- Fish TTS models/reference voice/latency controls: https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech
- ElevenLabs models/latency: https://elevenlabs.io/docs/overview/models
- ElevenLabs plans/credits: https://elevenlabs.io/pricing
