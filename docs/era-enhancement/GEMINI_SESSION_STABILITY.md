# Gemini Live session stability — evidence, root cause, plan

Status: **study complete, no code changed yet** (2026-11-11). Owner requirement:
stop making everything depend on one long-lived Live session, and never make him
wait through a reconnect. This document keeps the evidence so the fix is chosen
from measurements, not from the theory currently in the code.

---

## 1. What the logs actually show

Sources: `logs/eraone.log` (chronological stdout, 2026-11-06 14:09 → 11-11 21:21,
5.3 days) and `logs/era.log` (structured JSON, 11-10 → 11-11).

| Measurement | Value |
|---|---|
| Connect attempts / successful connects | 759 / 757 |
| Session rotations, **all 1008** | 631 |
| `engine.connection.closed` (era.log) | 255 |
| — of those, `1008 … client inactivity` | **246** |
| `conn_age_s` at those closes | **119.8–120.0** (a timer, not a variable) |
| Last mic path bucket before the close | `muted` 235, `standby` 8–11, **never `capture`** |
| Control: audio flowing, 20:19:56 | GoAway at `conn_age_s=539.9`, `time_left_s=50` |
| Handshake cost, 757 measured | min 1.01 s, median **2.21 s**, p90 3.45 s, p99 7.23 s, max 17.99 s |
| Continuity decisions after a drop | 281 "nothing was in flight" |

**The conclusion contradicts the comment in the code.** The rotation is not the
context window filling up (`app/era_live/tool_dispatch.py` used to say it was,
which is why context compression did not stop the drops). Google closes an idle
Live socket with 1008 after 120 s. Era's own mute and standby gates make the
socket idle: `_send_realtime` drops every frame (`audio.py:371-385`) and nothing
else is ever sent — no heartbeat, no silence frame, no ping.

---

## 2. Root cause table

| Cause | Detect | What happens | Status |
|---|---|---|---|
| **120 s client inactivity while muted/standby** | 1008 in `receive()` | classified "rotation" → immediate reconnect; 2–5 s deaf | **dominant (246/255), self-inflicted** |
| ~10 min connection lifetime | `response.go_away` | deferred to an idle boundary, then silent rotation | structural, unavoidable |
| 15 min audio-only session cap | — | moot: compression is enabled | handled |
| context fill | — | not observed as a close reason | theory was wrong |
| 1011 upstream | `_classify_disconnect` | local fallback 60 s | handled |
| 429 quota / auth / network | existing classifiers | key rotation, cooldown, local fallback | handled |
| resumption rejected | `_is_resumption_rejection` | drop handle once, retry clean | handled but handle age is 15 min vs 2 h allowed |
| deaf / mute / flaky | `live_health.verdict()` | `claude_voice` window, then local loop | handled |

Cost per drop: the user's speech during `reconnecting` is discarded
(`drop_reason="reconnecting"`), a mid-sentence reply stops, and nothing is
announced. The continuity attempt almost never has anything to replay.

---

## 3. API facts this plan relies on (verified from Google's docs)

- Audio-only Live sessions are limited to **15 minutes**; a **connection** lasts
  about **10 minutes**, with a `GoAway` carrying `timeLeft` before termination —
  [session management](https://ai.google.dev/gemini-api/docs/live-api/session-management).
- Session resumption: the server emits `SessionResumptionUpdate`;
  **tokens are valid 2 hours after the last session termination**; `resumable`
  is false while the model is executing function calls —
  [session management](https://ai.google.dev/gemini-api/docs/live-api/session-management).
- `contextWindowCompression`: `triggerTokens` defaults to 80 % of the window,
  `SlidingWindow.targetTokens` to trigger/2; discarding causes a temporary
  latency increase; system instructions always remain —
  [Live API reference](https://ai.google.dev/api/live).
- `clientContent` is appended to history and interrupts generation;
  `realtimeInput` streams continuously (audio/text/activity markers) —
  [Live API reference](https://ai.google.dev/api/live).
- Best practices: 20–40 ms audio chunks, 16 kHz, no client-side buffering,
  whole context re-billed per turn, audio ≈ 25 tokens/s —
  [best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices).

Measured fixed payload on our side: ~88 KB system prompt + ~84 KB tool
declarations before a single word, re-sent on every one of ~120 connects/day.

---

## 4. Five cheapest wins (G0 — accept before anything larger)

1. **Keepalive while muted/standby.** Send periodic *silence* (never mic audio)
   on the realtime input instead of dropping frames. This removes essentially
   all 246 observed drops and costs nothing. Privacy is preserved because the
   bytes are zeros, not microphone data.
2. **40 ms chunks.** `CHUNK_SIZE 1024 → 640` at 16 kHz matches the documented
   range and reduces per-frame overhead.
3. **Keep the resumption handle for its real lifetime.** 15 min → 2 h, and stop
   discarding it after two failed attempts (the server says it is still valid).
4. **Name the fault.** Split `1008-inactivity` from `1008-lifetime` in
   `_classify_disconnect` and log `conn_age_s` buckets, so this regression can
   never hide again.
5. **Buffer-and-resume, thin slice.** Keep the last ~3 s of PCM and the last
   utterance transcript across a drop; replay once after reconnect with an
   honest "I lost you for a second — go on." Never replay a confirmed action.

Acceptance for G0: a 24 h muted/standby soak produces **zero** idle-1008
closes; p95 reconnect < 3 s; 50 forced drops lose no user utterance.

---

## 5. Target architecture: one socket must never own anything the user would miss

```
mic ─▶ VAD/transport ─▶ Live session (ears/mouth)          ← small prompt, ~5 transport tools
                         │ ask_brain / run_tool / say
                         ▼
                   Brain (text model, tools, state)         ← owns reasoning + durable task state
                         │
                         ▼
                   task_store / report_outbox               ← survives any drop
```

- **Live keeps only** hearing, speaking, and three tools: `ask_brain`,
  `run_tool`, `say`. Its system prompt shrinks from ~88 KB to a few KB.
- **The brain** is a text model (`era.smart` / `era.local` / later the DeepSeek
  Harness worker) and owns tool choice, verification and durable state.
- **A drop loses no task state**, because none lives in the socket. Reconnect
  becomes invisible instead of a 2–5 s hole in the conversation.
- Side benefits: the fixed payload per turn collapses, cost falls, and the
  tool-schema problem in the audit stops being a Live-session constraint.

This is the structural fix. G0 buys time; G2 removes the dependency.

---

## 6. Phased plan

| Phase | Work | Acceptance |
|---|---|---|
| **G0** (½ day) | the five wins above | muted soak 24 h → 0 idle-1008; forced-drop test loses no utterance |
| **G1** (2–3 days) | resumption lifetime, fault naming, buffer-replay, honest "lost you" notice | 50 forced drops: one replay, no duplicate action, no stuck state |
| **G2** (1–2 wk) | transport/brain split on the typed path first (voice path unchanged) | typed task survives 5 forced drops with identical outcome |
| **G3** (2 wk) | Live becomes ears/mouth only; brain owns all tool execution | 7-day observation: 0 lost tasks, p95 first audio unchanged |

Do **not** build a warm standby session (G×): two sockets bill the audio context
twice, both go idle together, and the resumption handle is single-session.

---

## 7. Open questions (unverified)

- Whether a silent PCM frame counts as activity for the 120 s timer — inferred
  from the mute/standby correlation, not documented.
- `transparent` resumption is not in the public Gemini API reference.
- Server-side context caching for Live sessions is not documented.
- The real latency/cost delta of the transport/brain split (estimate +300–900 ms
  per reasoning hop; must be measured before G2 is accepted).
