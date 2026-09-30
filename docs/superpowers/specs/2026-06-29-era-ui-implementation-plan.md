# ERA ONE UI Animation System — Implementation Plan

**Date:** 2026-06-29
**Based on:** `2026-06-29-era-ui-animation-system-design.md`
**Status:** Ready to execute

---

## Implementation Order

Execute phases sequentially. Each phase produces a testable increment.

| Phase | What | Depends On | Est. Complexity |
|-------|------|------------|-----------------|
| 1 | System Monitor (Python → WebSocket → React) | None | Low |
| 2 | Voice Meter Fix (browser-side noise gate + VAD) | None | Medium |
| 3 | ParticleSphere Enhancements (standby, equalizer, quality) | None | High |
| 4 | Integration & Polish (wire everything, edge cases) | Phases 1-3 | Medium |

Phase 1 and 2 can be developed in parallel. Phase 3 is independent. Phase 4 requires all three.

---

## Phase 1: System Monitor — Real Metrics

### Step 1.1: Install psutil dependency

Add `psutil` to `requirements.txt` (or `requirements.windows.txt` if Windows-specific). Also add optional `pynvml` for NVIDIA GPU.

```
# requirements.txt (additions)
psutil>=5.9.0
pynvml>=11.5.0  # optional, for NVIDIA GPU
```

Verify installation: `pip install psutil pynvml`

### Step 1.2: Create `core/system_monitor.py`

Create new file at `C:\Users\nesri\OneDrive\Documents\EraOne\core\system_monitor.py`.

**Implementation notes:**
- Class `SystemMonitor` with `start()`, `stop()`, `get_latest()` methods
- Background `threading.Thread` for collection
- `_collect()` method returns `Dict[str, Any]`
- GPU: try `pynvml.nvmlInit()` in `__init__`, catch exception and set `_nvml_available = False`
- Wi-Fi check: iterate `psutil.net_if_addrs()`, look for 'wi-fi' or 'wlan' interface with AF_INET
- Network: store previous `net_io_counters()` values, compute delta per second for `net_sent_bytes_per_sec` and `net_recv_bytes_per_sec`
- If metric unavailable, set to `null` (Python None → JSON null)
- Thread is daemon so it dies with main process

**Key implementation detail:** `psutil.cpu_percent(interval=0.1)` blocks for 100ms. On first call it returns 0.0. Call it once at startup to prime it.

### Step 1.3: Wire SystemMonitor into `main.py`

- Import `SystemMonitor` from `core.system_monitor`
- Create instance: `system_monitor = SystemMonitor(interval=2.0)`
- Call `system_monitor.start()` after WebSocket server starts
- In the WebSocket message handler, add a new message type `'get_metrics'` that responds with `system_monitor.get_latest()`
- OR: send metrics proactively to connected clients every 2 seconds

**Proactive push approach (preferred):**
```python
async def push_metrics():
    while True:
        if ws_clients:
            metrics = system_monitor.get_latest()
            msg = json.dumps({'type': 'system_metrics', 'data': metrics})
            for client in ws_clients:
                await client.send(msg)
        await asyncio.sleep(2.0)
```

### Step 1.4: Update `useEra.ts`

- Add `metrics` state: `const [metrics, setMetrics] = useState<Record<string, any>>({});`
- In `ws.onmessage`, add case for `type: 'system_metrics'`:
  ```typescript
  case 'system_metrics':
    setMetrics(data.data);
    break;
  ```
- Export `metrics` from `useEra()` hook

### Step 1.5: Update `SystemMonitor.tsx`

- Remove all `useState` for cpu/mem/gpu/net (currently fake)
- Remove all `useEffect` with `setInterval` (currently simulating)
- Accept `metrics` prop from parent (or read from `useEra` context)
- Render values from `metrics` object:
  - `cpu_percent` → CPU row
  - `ram_percent` → MEM row
  - `net_sent_bytes_per_sec` / `net_recv_bytes_per_sec` → NET row (format as KB/s)
  - `gpu_usage_percent` → GPU row (show `N/A` if null)
  - `gpu_temp_celsius` → TMP row (show `N/A` if null)
  - `uptime_seconds` → UPT row (format as HH:MM)
  - `process_count` → PROC row
- Add visual stale indicator: if last update was > 5 seconds ago, dim the values

### Step 1.6: Test

- Start backend with `python main.py`
- Verify SystemMonitor panel shows real CPU/RAM values
- Verify GPU shows real value or N/A
- Verify NET shows actual throughput
- Verify values update every 2 seconds
- Verify no performance regression

---

## Phase 2: Voice Meter Fix + VAD Integration

### Step 2.1: Update `useAudioInput.ts` — Browser-side

**File:** `C:\Users\nesri\OneDrive\Documents\EraOne\ui_src\src\hooks\useAudioInput.ts`

Changes:
1. Update `getUserMedia` constraints:
   ```typescript
   const stream = await navigator.mediaDevices.getUserMedia({
     audio: {
       echoCancellation: true,
       noiseSuppression: true,
       autoGainControl: false,
     }
   });
   ```

2. Add noise gate constants:
   ```typescript
   const NOISE_GATE = 0.02;
   const ATTACK = 0.15;
   const RELEASE = 0.08;
   const SPEAKING_HOLD_MS = 200;
   ```

3. Add `smoothedVolume` variable (ref, not state):
   ```typescript
   const smoothedVolumeRef = useRef(0);
   ```

4. In the tick function, apply noise gate and smoothing:
   ```typescript
   const gated = avg > NOISE_GATE ? avg : 0;
   const speed = gated > smoothedVolumeRef.current ? ATTACK : RELEASE;
   smoothedVolumeRef.current += (gated - smoothedVolumeRef.current) * speed;
   setVolume(smoothedVolumeRef.current);
   setPeakVolume(peak / 255);
   setIsSpeaking(smoothedVolumeRef.current > NOISE_GATE);
   ```

5. Keep existing `setTimeout` for speaking hold.

6. Export additional value: `userVoiceConfirmed: boolean` — will be set from Python VAD state (see Step 2.3).

### Step 2.2: Create Python VAD module

**New file:** `C:\Users\nesri\OneDrive\Documents\EraOne\core\vad.py`

**Implementation notes:**
- Use `webrtcvad` Python package (add to `requirements.txt`: `webrtcvad>=2.0.10`)
- `webrtcvad` requires 16-bit PCM audio at 8000/16000/32000/48000 Hz
- Strategy: receive audio chunks from browser over WebSocket, process through VAD
- Alternative simpler approach: have Python detect speaking from WebSocket state messages (when user sends voice data)
- If `webrtcvad` install is problematic on Windows, fallback to simple amplitude threshold on Python side

**Simplified approach (recommended):**
- Don't stream raw audio to Python (complex, adds latency)
- Instead: Python VAD runs on the backend, receiving mic toggle events
- When user activates mic, Python sends `vad_enabled: true`
- Python monitors WebSocket for audio data if user enables "high accuracy mode"
- For initial implementation: trust browser-side `isSpeaking` but add Python-side confirmation via state machine logic

### Step 2.3: WebSocket protocol update

Add new message type:
```json
{
  "type": "vad_state",
  "confirmed": true/false
}
```

- Python sends `vad_state` with `confirmed: true` when it believes user is speaking (based on state machine: if `activity === 'listening'` and user sends mic data, mark as confirmed)
- React updates `useAudioInput` to dim bars when `confirmed === false` but local `isSpeaking === true`

### Step 2.4: Update `VoiceMeter.tsx`

**File:** `C:\Users\nesri\OneDrive\Documents\EraOne\ui_src\src\components\VoiceMeter.tsx`

Changes:
- Accept new prop: `vadConfirmed: boolean`
- When `vadConfirmed === false` and `micActive === true` and bars are active:
  - After 300ms timeout, dim bars (reduce opacity to 0.3)
  - Add subtle visual indicator that background noise is being ignored
- When `vadConfirmed === true`:
  - Bars glow brighter (increase saturation)
  - Show "VOICE DETECTED" text briefly

### Step 2.5: Update `App.tsx` to wire VAD state

- Pass `vadConfirmed` state down to `VoiceMeter`
- Source from `useEra` metrics or new `vad_state` message

### Step 2.6: Test

- Play music on computer, open ERA
- Voice meter bars should NOT move when music plays
- Speak into mic → bars should move
- If music is loud and user speaks, bars should react only to voice (not music)
- Verify no audio feedback loops

---

## Phase 3: ParticleSphere Enhancements

### Step 3.1: Add quality presets

**File:** `C:\Users\nesri\OneDrive\Documents\EraOne\ui_src\src\components\ParticleSphere.tsx`

Changes:
1. Add quality types and constants:
   ```typescript
   type QualityPreset = 'low' | 'medium' | 'high';
   
   const QUALITY_PRESETS = {
     low: { dots: 400, maxFps: 30, sortInterval: 3, outerRing: false },
     medium: { dots: 700, maxFps: 60, sortInterval: 2, outerRing: true },
     high: { dots: 1200, maxFps: 60, sortInterval: 2, outerRing: true },
   };
   ```

2. Add `quality` prop or read from context:
   ```typescript
   interface Props {
     mode: SphereMode;
     intensity: number;
     quality?: QualityPreset;
   }
   ```

3. In useEffect, when quality changes, rebuild fibonacci points:
   ```typescript
   function rebuildDots(count: number) {
     const rebuilt = fibonacciSphere(count);
     theta = rebuilt.theta;
     phi = rebuilt.phi;
     sinT = rebuilt.sinT;
     cosT = rebuilt.cosT;
     idx = new Int32Array(count);
     zBuf = new Float32Array(count);
     px = new Float32Array(count);
     py = new Float32Array(count);
     for (let i = 0; i < count; i++) idx[i] = i;
   }
   ```

### Step 3.2: Standby mode enhancements

Changes to render function:
1. When `standby === true`:
   - Reduce `radius` by 60% (sphere is 40% of active size):
     ```typescript
     const standbyScale = 0.4;
     const radius = BASE_RADIUS * scaleFactor * (standby ? standbyScale : 1.0);
     ```
   - Color palette: use red hues (340-360°) instead of cyan:
     ```typescript
     if (standby) {
       hue = 350 + Math.sin(theta[i] * 5 + time) * 18;
       sat = 90;
       lit = 35 + depth01 * 25;
     }
     ```
   - Wave displacement: lock to zero or near-zero:
     ```typescript
     const displacement = standby ? Math.sin(time * 0.5) * 0.01 : wave1 + wave2 + wave3 + ...;
     ```
   - Rotation speed: reduce to 10%:
     ```typescript
     const rotY = time * (standby ? 0.1 : 0.45);
     const rotX = Math.sin(time * 0.28) * (standby ? 0.05 : 0.25);
     ```
   - Outer ring: red instead of cyan:
     ```typescript
     if (standby) {
       ringGrad.addColorStop(0, 'rgba(220,38,38,0.08)');
       ringGrad.addColorStop(1, 'rgba(220,38,38,0)');
     }
     ```

### Step 3.3: Wake transition (standby → active)

- When `mode` changes from `standby` to `active`:
  - `smoothIntensity` starts at 0 and lerps to target over 600-800ms
  - `standbyScale` lerps from 0.4 to 1.0 using cubic ease-out
  - Implement as:
    ```typescript
    let targetScale = standby ? 0.4 : 1.0;
    let currentScale = useRef(0.4);
    
    // In render loop:
    const scaleDiff = targetScale - currentScale.current;
    currentScale.current += scaleDiff * 0.05; // ~600ms lerp
    const radius = BASE_RADIUS * scaleFactor * currentScale.current;
    ```

### Step 3.4: Speaking equalizer harmonics

When `smoothIntensity > 0.7` (speaking state):
```typescript
let eq = 0;
if (smoothIntensity > 0.7) {
  eq = Math.sin(theta[i] * 3 + time * 5.5 + phi[i] * 1.5) * 0.06 * smoothIntensity;
  eq += Math.sin(theta[i] * 9 - time * 3.2 + phi[i] * 5) * 0.04 * smoothIntensity;
  eq += Math.sin(phi[i] * 7 + time * 4.0) * 0.03 * smoothIntensity;
}
const displacement = wave1 + wave2 + wave3 + eq + smoothIntensity * 0.34;
```

### Step 3.5: Adaptive quality manager

Add FPS tracking in render loop:
```typescript
let fpsSamples: number[] = [];
let currentQuality: QualityPreset = 'medium';

// After drawing each frame:
const elapsed = performance.now() - frameStart;
fpsSamples.push(1000 / elapsed);

// Every 60 frames (~1s at 60fps):
if (fpsSamples.length >= 60) {
  const avgFps = fpsSamples.reduce((a, b) => a + b, 0) / fpsSamples.length;
  
  if (avgFps > 50 && currentQuality !== 'high') {
    currentQuality = currentQuality === 'low' ? 'medium' : 'high';
    rebuildDots(QUALITY_PRESETS[currentQuality].dots);
  } else if (avgFps < 30 && currentQuality !== 'low') {
    currentQuality = currentQuality === 'high' ? 'medium' : 'low';
    rebuildDots(QUALITY_PRESETS[currentQuality].dots);
  }
  
  fpsSamples = [];
}
```

### Step 3.6: Window visibility handling

```typescript
useEffect(() => {
  const handleVisibility = () => {
    if (document.hidden) {
      // Reduce intensity, pause sort, lower FPS
      intensityRef.current = 0.1;
    } else {
      // Resume normal intensity
      intensityRef.current = intensity;
    }
  };
  
  document.addEventListener('visibilitychange', handleVisibility);
  return () => document.removeEventListener('visibilitychange', handleVisibility);
}, [intensity]);
```

### Step 3.7: Sort amortization

Instead of sorting every frame, sort every N frames:
```typescript
const sortInterval = QUALITY_PRESETS[currentQuality].sortInterval;

// In render loop:
if (frameCount % sortInterval === 0) {
  idx.sort((a, b) => zBuf[a] - zBuf[b]);
}
```

### Step 3.8: Test

- Standby mode: sphere should be small, red, minimal animation
- Wake transition: smooth growth from 40% to 100% over ~700ms
- Speaking mode: sphere pulses with equalizer harmonics
- FPS monitoring: if FPS drops, dot count reduces automatically
- Window hidden: animation pauses or reduces significantly
- Long running (30+ min): no memory leaks, no FPS degradation

---

## Phase 4: Integration & Polish

### Step 4.1: Wire Python VAD to React state

- When Python sends `user_voice_active: true`, React should:
  - Set sphere intensity to speaking level (0.85+)
  - Show `SPEAKING` state in status indicator
  - Make VoiceMeter bars glow brighter
- When Python sends `user_voice_active: false`, React should:
  - If activity was `listening`, transition to `idle`
  - Reduce sphere intensity
  - VoiceMeter bars return to normal

### Step 4.2: End-to-end state machine test

Test full cycle:
1. Start in standby → sphere small, red
2. Toggle standby off → sphere grows to active
3. User speaks → sphere shows listening undulation
4. ERA responds → sphere shows speaking equalizer
5. ERA finishes → sphere returns to listening
6. Toggle standby on → sphere shrinks back to standby

### Step 4.3: Edge case testing

| Scenario | Expected Behavior |
|----------|-------------------|
| Mic denied by browser | Show "Mic OFF" indicator, VoiceMeter disabled, sphere in idle |
| GPU unavailable | SystemMonitor shows N/A for GPU rows |
| WebSocket disconnect | SystemMonitor shows stale values, reconnection attempt every 3s |
| Window minimized | ParticleSphere pauses, Python metrics continue |
| Low FPS device | Auto-detect, force low quality mode |
| High CPU usage | SystemMonitor thread doesn't block main UI |
| Python VAD not installed | Fallback to browser-side isSpeaking, log warning |

### Step 4.4: Performance verification

- Run on mid-range device (e.g., Intel i5, 8GB RAM, GTX 1060)
- Verify CPU usage stays < 5% for ERA UI process
- Verify no jank during speaking transitions
- Verify standby mode uses minimal resources
- Run for 1+ hour, check for memory leaks

### Step 4.5: Documentation update

- Update `readme.md` with new dependencies (psutil, pynvml, webrtcvad)
- Add section on system requirements
- Document low-performance mode toggle

---

## File Change Summary

| File | Phase | Action | Description |
|------|-------|--------|-------------|
| `requirements.txt` | 1 | Edit | Add psutil, pynvml |
| `core/system_monitor.py` | 1 | Create | System metrics collector |
| `main.py` | 1 | Edit | Start SystemMonitor, push metrics |
| `useEra.ts` | 1 | Edit | Parse system_metrics message, expose metrics |
| `SystemMonitor.tsx` | 1 | Edit | Render real metrics from props |
| `useAudioInput.ts` | 2 | Edit | Noise gate, smoothing, constraints |
| `core/vad.py` | 2 | Create | VAD wrapper (if webrtcvad available) |
| `VoiceMeter.tsx` | 2 | Edit | Dim bars when VAD says no voice |
| `App.tsx` | 2 | Edit | Wire vadConfirmed state |
| `ParticleSphere.tsx` | 3 | Edit | Quality presets, standby, equalizer |
| `index.css` | 3 | Edit | Add standby-specific animations if needed |

---

## Dependencies Added

```txt
# Python
psutil>=5.9.0
pynvml>=11.5.0  # optional, for NVIDIA GPU
webrtcvad>=2.0.10  # optional, for Python VAD
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| pynvml fails on some NVIDIA drivers | Medium | Low | Graceful fallback to N/A |
| webrtcvad difficult to install on Windows | Medium | Medium | Use browser-side VAD as primary, Python VAD as enhancement |
| psutil.cpu_percent blocks for 100ms | Low | Low | Call with interval=0.1, not in hot path |
| Adaptive quality causes visual pop | Low | Low | Smooth transition when changing dot count |
| WebSocket message flood from metrics | Low | Low | 2s interval is conservative |

---

**End of Implementation Plan**
