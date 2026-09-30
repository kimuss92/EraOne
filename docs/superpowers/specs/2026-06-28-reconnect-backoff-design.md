# Streaming Reconnect Backoff & Standby Design

## Problem
The persistent streaming connection to Gemini's Live API crashes in a rapid retry loop when the server sends a `GoAway` frame or a connection drops. The crash is caused by:
1. No jitter on exponential backoff — retries land at exact intervals, flooding the server
2. `_reconnect_attempts` resets to 0 on every `async with` entry (line 2219), so backoff never grows beyond 3s
3. After 10 failed attempts, a 5-minute blocking sleep freezes the entire assistant
4. Reconnection greetings trigger audible TTS on every network blip, creating a noisy user experience

## Solution

### 1. Exponential Backoff with Jitter
- Formula: `min(3.0 * (2 ** attempt_number), 60.0) + random.uniform(0.1, 1.5)`
- Yields delays: ~3.1–4.5s, ~6.1–7.5s, ~12.1–13.5s, ~24.1–25.5s, ~48.1–49.5s, ~60.1–61.5s

### 2. Standby at Max Retries
- Max retries: 6 (replaces old 10-attempt / 5-min cooldown)
- On hit: `enter_standby()` transitions the system to STANDBY state
- Wake-word listener stays active — user says "Jarvis" to resume

### 3. GoAway → Backoff Routing
- Removed `self._reconnect_attempts = 0` from inside `async with` block
- Backoff counter now accumulates across connection attempts
- Resets only on: `_exit_standby()` (F4) or wake-word detection

### 4. UX: First Boot vs Silent Reconnect
- New flag: `_has_completed_initial_boot`
- First boot: audible greeting "Jarvis is online and ready, sir."
- Subsequent reconnects: silent UI update only

### 5. Standby Notifications
- Enter standby: `speak("Standby mode active")`
- Exit standby (F4/wake): `speak("Back online, sir.")` (updated 2026-07-02 to match the user's recorded `standby_resume_style` preference; was originally "Awaiting commands.")

## Files Modified
- `main.py` — `JarvisLive` class: `__init__`, `_compute_reconnect_delay()`, `run()`, `enter_standby()`, `_exit_standby()`, wake word listener notification
