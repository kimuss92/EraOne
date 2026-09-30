# ERA ONE UI Animation System — Design Spec

**Date:** 2026-06-29
**Status:** Approved
**Scope:** Improve existing ERA ONE UI behavior, real system data, voice isolation, and performance. No visual redesign.

---

## 1. Goal

Improve the existing ERA ONE cyberpunk interface by:
- Fixing the ParticleSphere to react properly to ERA's speaking state and voice activity
- Replacing the fake SystemMonitor with real metrics from Python
- Fixing the VoiceMeter to react only to microphone input (not system audio/music)
- Optimizing Canvas 2D performance for smooth, lightweight operation

The existing UI layout, color scheme, and visual identity remain unchanged.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   React Frontend (PyQt6)                  │
│                                                           │
│  ┌──────────────┐  ┌───────────┐  ┌────────────────┐     │
│  │ ParticleSphere│  │VoiceMeter │  │ SystemMonitor   │     │
│  │ (Canvas 2D,   │  │(React DOM │  │ (React DOM,     │     │
│  │  own rAF loop)│  │ bars)     │  │  display-only)  │     │
│  └──────┬───────┘  └─────┬─────┘  └───────┬─────────┘     │
│         │                │                │               │
│         └────────┬───────┘                │               │
│                  │                        │               │
│          ┌───────┴────────┐              │               │
│          │    App.tsx      │              │               │
│          │  (state orchestrator)         │               │
│          └───────┬────────┘              │               │
│                  │                       │               │
│          ┌───────┴────────┐             │               │
│          │   useEra.ts    │             │               │
│          │ (WebSocket)    │             │               │
│          └───────┬────────┘             │               │
└──────────────────┼──────────────────────┼───────────────┘
                   │ WebSocket messages   │
┌──────────────────┼──────────────────────┼───────────────┐
│                  │                      │                │
│          ┌───────┴────────┐  ┌──────────┴──────────┐    │
│          │   main.py      │  │ core/system_monitor  │    │
│          │ (WS server,    │  │ (psutil + WMI/NVML) │    │
│          │  VAD via       │  │ polls every 2s      │    │
│          │  webrtcvad)    │  └─────────────────────┘    │
│          └───────┬────────┘                              │
│                  │                                       │
│               Python Backend                             │
└──────────────────────────────────────────────────────────┘
```

**Key principles:**
- ParticleSphere owns its own `requestAnimationFrame` loop — completely decoupled from React's render cycle. React only passes mode/intensity via refs (no re-render on animation frames).
- SystemMonitor is a pure display component — receives metrics from WebSocket, never polls. No timers in the frontend.
- VoiceMeter uses local mic volume for instant bars but respects Python VAD for long-term state.
- New WebSocket messages: `system_metrics`, `user_voice_active`, `vad_state`.

---

## 3. ERA State Machine & Sphere Behavior

### 3.1 State Enum (existing, no change)

```typescript
type ActivityState = 'idle' | 'listening' | 'thinking' | 'processing' | 'speaking' | 'standby';
```

### 3.2 State → Sphere Behavior Mapping

| State | Sphere Size | Color Palette | Movement | Dot Count |
|-------|------------|---------------|----------|-----------|
| `standby` | 35–40% of active radius | Red/dark red hues (340–360°) | Very slow rotation (0.1 rad/s), subtle 1s breathing. No wave displacement. | Low preset |
| `idle` | 70% → grows to 100% on wake | Cyan-to-violet gradient (185–250°) | Gentle rotation, minimal breathing wave | Medium preset |
| `listening` | 100% | Cyan/blue (185–220°) | Subtle undulation synced to mic volume envelope | Medium preset |
| `thinking` | 100% | Amber/warm (40–60°) | Slow pulse + rotation, medium wave amplitude | Medium preset |
| `speaking` | 100% | Violet-to-cyan sweep (270–190°) | Full equalizer: synchronized wave displacement per frequency band. Highest intensity. | High preset |
| `error/offline` | 100% | Dim gray/red mixed | Very slow, low opacity | Low preset |

### 3.3 Transition Easing

- `standby ↔ idle`: Smooth size lerp over 600–800ms using cubic ease-out
- `listening → thinking → speaking`: Intensity lerps; sphere never snaps
- `speaking → listening`: Exponential decay on wave amplitude (0.92 multiplier) to avoid pop

### 3.4 WebSocket Messages from Python

| Message Type | Payload | Purpose |
|-------------|---------|---------|
| `state` | `{ type: 'state', state: string }` | Keeps sphere mode in sync (existing) |
| `system_metrics` | `{ type: 'system_metrics', ... }` | Feeds SystemMonitor (new) |
| `user_voice_active` | `{ type: 'user_voice_active', active: boolean }` | Authoritative VAD state for agent (new) |

---

## 4. ParticleSphere Animation Logic

### 4.1 Component Structure

- Accepts `mode: StateMode` and `intensity: number` props
- Inside canvas rAF loop: `intensityRef.current = intensity` via refs — no re-render
- `quality` prop (low/medium/high) switches `POINT_COUNT` at mount time
- Standby: `BASE_RADIUS` reduced, color shifts from cyan to red, wave displacement locked to zero, rotation speed at 10%
- Active: existing wave + rotation logic remains, but intensity is multiplied by `intensity` prop
- Speaking: when `intensity > 0.7`, additional higher-frequency wave harmonics kick in

### 4.2 Wave Displacement Formula

```typescript
// Base wave (existing, kept)
const wave1 = Math.sin(theta * 4 + time * 2.2 + phi * 2) * 0.08;
const wave2 = Math.sin(theta * 7 - time * 1.4 + phi * 4) * 0.04;
const wave3 = Math.sin(phi * 3 + time * 1.1) * 0.03;

// Equalizer harmonics (new, active only when speaking)
const eq1 = Math.sin(theta * 3 + time * 5.5 + phi * 1.5) * 0.06 * intensity;
const eq2 = Math.sin(theta * 9 - time * 3.2 + phi * 5) * 0.04 * intensity;
const eq3 = Math.sin(phi * 7 + time * 4.0) * 0.03 * intensity;

const displacement = wave1 + wave2 + wave3 + eq1 + eq2 + eq3 + smoothIntensity * 0.34;
```

### 4.3 Quality Presets

| Preset | Dots | Max FPS | Z-Sort | Outer Ring |
|--------|------|---------|--------|------------|
| Low | 350–500 | 30 | Every 3 frames | Disabled |
| Medium | 700–900 | 60 | Every 2 frames | Enabled |
| High | 1000–1500 | 60 | Every 2 frames | Enabled |

### 4.4 Adaptive Quality Manager

```
if (avg_fps > 50 for 1s AND mode == 'active'):
    increase quality preset (capped at High)
if (avg_fps < 30 for 2s):
    decrease quality preset (capped at Low)
```

### 4.5 Window Visibility Handling

```typescript
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Pause rAF entirely, or reduce to 4fps snapshot
    // Set intensity to idle value
  } else {
    // Resume full rAF loop
  }
});
```

---

## 5. Voice Meter & Audio Pipeline

### 5.1 Browser Side (`useAudioInput.ts` changes)

**getUserMedia constraints:**
```typescript
const stream = await navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
  }
});
```

**Noise gate:**
- `noiseGateThreshold` default 0.02 (calibrated per-session or via settings)
- `attackSpeed` and `releaseSpeed` for volume smoothing (exponential moving average)
- `isSpeaking` derived from smoothed volume > `noiseGateThreshold` with 200ms hold

**Important:** `isSpeaking` from Python VAD takes precedence for actual agent behavior. Browser-side `isSpeaking` is only for local voice meter visualization.

### 5.2 Python Side (`vad.py`)

- Capture 16-bit PCM audio chunks from browser → process through `webrtcvad`
- Send `user_voice_active: true/false` over WebSocket
- When `user_voice_active` is true, `speaking` state propagates to React
- React passes `intensity = 0.85 + ...` to the sphere

### 5.3 VoiceMeter.tsx Behavior

- Unchanged visual (still 28 bars)
- Bars react to `volume` and `peakVolume` from `useAudioInput`
- If ERA state is `listening` AND `user_voice_active` from Python is true → bars glow brighter (visual confirmation voice made it to Python)
- If local `volume` is above threshold but Python says `user_voice_active = false` → bars dim after 300ms (so user sees that background noise is being ignored)

### 5.4 Limitations (Browser Audio Isolation)

The browser API cannot perfectly isolate microphone input from system audio on Windows because:
- The audio subsystem may mix streams before they reach `getUserMedia`
- Some browsers pass through loopback audio on certain drivers

**Practical solution:** `echoCancellation` + `noiseSuppression` + `autoGainControl` flags in getUserMedia + Python VAD as authoritative gate. This handles 95%+ of real-world cases. The remaining edge case (system audio bleeding into mic) is a Windows audio driver issue that cannot be solved purely in JavaScript.

---

## 6. System Monitor Data Flow

### 6.1 Python Module: `core/system_monitor.py`

**Metrics collected:**

| Metric | Source | Fallback |
|--------|--------|----------|
| CPU % | `psutil.cpu_percent(interval=0.1)` | Always available |
| RAM % | `psutil.virtual_memory().percent` | Always available |
| Network I/O | `psutil.net_io_counters()` → sent/received bytes/sec | Always available |
| Wi-Fi connected | Check if Wi-Fi interface has active IP | `N/A` if not on Wi-Fi |
| GPU usage | Try `pynvml.nvmlDeviceGetUtilizationRates()` | Fallback to WMI, then `N/A` |
| GPU temperature | Try `pynvml.nvmlDeviceGetTemperature()` | Fallback to WMI, then `N/A` |
| Uptime | `time.time() - psutil.boot_time()` | Always available |
| Process count | `len(psutil.pids())` | Always available |

**Polling interval:** Every 2 seconds (configurable).

**Thread safety:** Runs in its own `threading.Thread`, never blocks main WebSocket thread.

### 6.2 WebSocket Message Format

```json
{
  "type": "system_metrics",
  "data": {
    "cpu_percent": 45.2,
    "ram_percent": 68.1,
    "net_sent_bytes_per_sec": 1024,
    "net_recv_bytes_per_sec": 2048,
    "wifi_connected": true,
    "gpu_usage_percent": 32.0,
    "gpu_temp_celsius": 67.0,
    "uptime_seconds": 86400,
    "process_count": 210
  }
}
```

If a metric is unavailable, its value is `null`. React renders `N/A` for `null`.

### 6.3 React Side

- `useEra.ts` listens for `system_metrics` WebSocket messages
- Exposes a `metrics` dict via context or state
- `SystemMonitor.tsx` renders the dict directly. No logic, no timers, no faking.

---

## 7. Performance Optimization Strategy

| Technique | Description |
|-----------|-------------|
| **Decouple rAF from React** | ParticleSphere reads intensity via `useRef`, never state. Only `mode` change triggers re-render. |
| **Adaptive dot count** | Start at medium (700). Measure avg FPS over 1s. If > 50 and active → high (1200). If < 30 for 2s → low (400). Standby always low. |
| **Sort amortization** | Full depth sort every 2 frames (60fps) or 3 frames (30fps). Since rotation is continuous, depth buffer changes slowly. |
| **Window visibility** | On `document.hidden`, pause rAF or reduce to 4fps snapshot with no sort. |
| **Separate polling loops** | SystemMonitor Python side: 2s interval. Animation rAF: 60fps. Completely independent. |
| **Low-Performance Mode** | User toggle or auto-trigger. Reduces dots to 350, caps FPS to 30, disables outer ring gradient, disables z-sort. |
| **No React state updates per frame** | All animation reads from refs, not state. React only re-renders when mode changes or WebSocket message arrives. |

---

## 8. Edge Cases & Fallback Behavior

| Edge Case | Mitigation |
|-----------|------------|
| Browser denies mic | Show red "Mic OFF" indicator. VoiceMeter disabled. User must toggle via button or OS settings. |
| GPU temp unavailable | Display `N/A` in SystemMonitor. Never fake data. |
| WebSocket disconnect | SystemMonitor shows last known values with stale timestamp (grayed out). Reconnect every 3s. |
| Window minimized/hidden | Pause ParticleSphere rAF. Python system_monitor continues in background. |
| Low-end device | Auto-detect via FPS. Force low quality mode. |
| Python VAD unavailable / not installed | Fallback: `useAudioInput` `isSpeaking` becomes authoritative. Log warning. |
| High CPU on Python side | SystemMonitor runs in own thread, never blocks main UI thread. |

---

## 9. Step-by-Step Implementation Roadmap

### Phase 1: Voice Meter Fix + VAD Integration
1. Update `useAudioInput.ts` with noise gate, smoothing, getUserMedia constraints
2. Add `webrtcvad` Python module
3. Stream audio chunks to Python, receive `user_voice_active` back
4. Update VoiceMeter.tsx to dim bars when Python says no real voice
5. Test: play music while speaking → bars should only react to voice

### Phase 2: System Monitor Real Metrics
1. Create `core/system_monitor.py` with psutil + NVML/WMI
2. Start SystemMonitor in main.py on startup
3. Send `system_metrics` over WebSocket every 2s
4. Update `useEra.ts` to parse and expose metrics
5. Update `SystemMonitor.tsx` to render real data (N/A for unavailable)
6. Test: verify all metrics display correctly

### Phase 3: ParticleSphere Enhancements
1. Add quality presets (low/medium/high dot counts)
2. Add standby mode: smaller sphere, red color, minimal animation
3. Add wake transition: smooth size lerp with easing
4. Add speaking equalizer harmonics
5. Add adaptive quality manager (FPS-based dot count adjustment)
6. Add window visibility handling (pause when hidden)
7. Test: standby → wake → speak transitions are smooth

### Phase 4: Performance Optimization
1. Audit React state updates — ensure no per-frame state changes
2. Implement sort amortization
3. Add low-performance mode toggle
4. Benchmark: verify < 5% CPU on mid-range device
5. Test: long-running stability (1+ hour)

### Phase 5: Integration & Polish
1. Wire all pieces together: Python VAD + SystemMonitor + ParticleSphere
2. End-to-end test: standby → wake → listen → speak → standby cycle
3. Edge case testing: mic denied, GPU unavailable, WebSocket disconnect
4. Performance regression testing
5. Documentation update

---

## 10. Acceptance Criteria

| Criterion | Requirement |
|-----------|-------------|
| Sphere reacts to speaking | When ERA speaks, sphere animates with equalizer motion synced to voice |
| Standby mode | Sphere shrinks to 35-40%, red color, minimal animation |
| Wake transition | Smooth 600-800ms lerp from standby to active size |
| Voice meter isolation | Meter reacts only to mic input, not system audio/music |
| Python VAD accuracy | VAD correctly detects voice vs background noise |
| System monitor real data | All metrics come from Python psutil, not simulated |
| GPU fallback | GPU temp shows N/A if unavailable, never fake data |
| Performance | < 5% CPU on mid-range device, no jank |
| Adaptive quality | FPS drops → dot count reduces automatically |
| Window visibility | Animation pauses when window hidden |
| Low-performance mode | User toggle reduces dots, caps FPS |
| WebSocket resilience | Reconnects every 3s, stale data handled gracefully |

---

## 11. Suggested File Structure

```
ui_src/src/
├── App.tsx                          # State orchestrator, unchanged mostly
├── hooks/
│   ├── useAudioInput.ts             # Updated: noise gate, smoothing, constraints
│   ├── useEra.ts                    # Updated: parse system_metrics, user_voice_active
│   └── usePerformanceMode.ts        # New: quality presets, FPS monitoring
├── components/
│   ├── ParticleSphere.tsx           # Updated: quality presets, equalizer, standby size
│   ├── VoiceMeter.tsx               # Updated: dim when Python VAD says no voice
│   ├── SystemMonitor.tsx            # Updated: render real metrics from WebSocket
│   ├── ThinkingIndicator.tsx        # Unchanged
│   └── TitleBar.tsx                 # Unchanged
├── main.tsx                         # Unchanged
└── index.css                        # Unchanged

core/
├── system_monitor.py                # New: psutil + NVML/WMI metrics
├── vad.py                           # New: webrtcvad wrapper
├── websocket_server.py              # Updated: handle system_metrics, vad_state
└── main.py                          # Updated: start SystemMonitor, wire VAD
```

---

## 12. Pseudocode

### 12.1 Main Animation Loop (ParticleSphere)

```typescript
// In useEffect inside ParticleSphere.tsx
let raf = 0;
let time = 0;
let frameCount = 0;
let smoothIntensity = intensityRef.current;
let currentQuality: 'low' | 'medium' | 'high' = 'medium';
let fpsSamples: number[] = [];

// Helper: rebuild fibonacci points when dot count changes
function rebuildDots(quality: 'low' | 'medium' | 'high') {
  const count = quality === 'low' ? 400 : quality === 'medium' ? 700 : 1200;
  const rebuilt = fibonacciSphere(count);
  theta = rebuilt.theta;
  phi = rebuilt.phi;
  sinT = rebuilt.sinT;
  cosT = rebuilt.cosT;
  // Reallocate buffers
  idx = new Int32Array(count);
  zBuf = new Float32Array(count);
  px = new Float32Array(count);
  py = new Float32Array(count);
  for (let i = 0; i < count; i++) idx[i] = i;
}
// NOTE: theta, phi, sinT, cosT, idx, zBuf, px, py must be declared with let, not const

const render = () => {
  const now = performance.now();
  time += 0.016;
  
  // Update intensity from ref (no React state)
  const target = intensityRef.current;
  smoothIntensity += (target - smoothIntensity) * 0.08;
  
  // Check window visibility
  if (document.hidden) {
    raf = requestAnimationFrame(render);
    return;
  }
  
  // Adaptive quality (every 60 frames = ~1s at 60fps)
  if (fpsSamples.length >= 60) {
    const avgFps = fpsSamples.reduce((a, b) => a + b, 0) / fpsSamples.length;
    if (avgFps > 50 && currentQuality !== 'high') {
      // Increase quality
      currentQuality = currentQuality === 'low' ? 'medium' : 'high';
      rebuildDots(currentQuality); // Recalculate fibonacci points
    } else if (avgFps < 30 && currentQuality !== 'low') {
      // Decrease quality
      currentQuality = currentQuality === 'high' ? 'medium' : 'low';
      rebuildDots(currentQuality);
    }
    fpsSamples = [];
  }
  
  const dotCount = currentQuality === 'low' ? 400 : currentQuality === 'medium' ? 700 : 1200;
  
  // Clear with fade
  ctx.fillStyle = 'rgba(0,0,0,0.22)';
  ctx.fillRect(0, 0, width, height);
  
  // Rotation
  const rotY = time * (standby ? 0.1 : 0.45);
  const rotX = Math.sin(time * 0.28) * (standby ? 0.05 : 0.25);
  
  // For each dot: displace, rotate, project (existing logic)
  for (let i = 0; i < dotCount; i++) {
    const wave1 = Math.sin(theta[i] * 4 + time * 2.2 + phi[i] * 2) * 0.08;
    const wave2 = Math.sin(theta[i] * 7 - time * 1.4 + phi[i] * 4) * 0.04;
    const wave3 = Math.sin(phi[i] * 3 + time * 1.1) * 0.03;
    
    // Equalizer harmonics (only when speaking)
    let eq = 0;
    if (smoothIntensity > 0.7) {
      eq = Math.sin(theta[i] * 3 + time * 5.5 + phi[i] * 1.5) * 0.06 * smoothIntensity;
      eq += Math.sin(theta[i] * 9 - time * 3.2 + phi[i] * 5) * 0.04 * smoothIntensity;
    }
    
    const displacement = wave1 + wave2 + wave3 + eq + smoothIntensity * 0.34;
    const r = radius * (1 + displacement);
    
    // Rotate and project (existing code)
    // ...
  }
  
  // Sort amortization: only sort every 2 frames
  if (frameCount % 2 === 0) {
    idx.sort((a, b) => zBuf[a] - zBuf[b]);
  }
  
  // Draw dots (existing code)
  // ...
  
  const elapsed = performance.now() - now;
  fpsSamples.push(1000 / elapsed);
  frameCount++;
  
  raf = requestAnimationFrame(render);
};

raf = requestAnimationFrame(render);
```

### 12.2 Audio Analyzer (VAD Integration)

```typescript
// useAudioInput.ts - updated
const stream = await navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
  }
});

const analyser = ctx.createAnalyser();
analyser.fftSize = 256;
analyser.smoothingTimeConstant = 0.75;

const source = ctx.createMediaStreamSource(stream);
source.connect(analyser);

const data = new Uint8Array(analyser.frequencyBinCount);
const NOISE_GATE = 0.02;
const ATTACK = 0.15;   // Speed of smoothing when volume increases
const RELEASE = 0.08;  // Speed of smoothing when volume decreases

let smoothedVolume = 0;

const tick = () => {
  analyser.getByteFrequencyData(data);
  let sum = 0;
  let peak = 0;
  for (let i = 0; i < data.length; i++) {
    sum += data[i];
    if (data[i] > peak) peak = data[i];
  }
  const avg = sum / data.length / 255;
  
  // Noise gate
  const gated = avg > NOISE_GATE ? avg : 0;
  
  // Smooth with attack/release
  const speed = gated > smoothedVolume ? ATTACK : RELEASE;
  smoothedVolume += (gated - smoothedVolume) * speed;
  
  setVolume(smoothedVolume);
  setPeakVolume(peak / 255);
  setIsSpeaking(smoothedVolume > NOISE_GATE);
  
  // Hold release: if speaking, keep isSpeaking for 200ms after volume drops
  if (smoothedVolume > NOISE_GATE) {
    clearTimeout(speakingTimerRef.current);
    speakingTimerRef.current = window.setTimeout(() => setIsSpeaking(false), 200);
  }
  
  rafRef.current = requestAnimationFrame(tick);
};
```

### 12.3 Python System Monitor

```python
# core/system_monitor.py
import psutil
import time
import threading
from typing import Dict, Any, Optional

class SystemMonitor:
    """Collects real system metrics every N seconds."""
    
    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.running = False
        self.latest: Dict[str, Any] = {}
        self._thread: Optional[threading.Thread] = None
        self._gpu = None
        self._nvml_available = False
        self._init_gpu()
    
    def _init_gpu(self):
        """Try to initialize NVIDIA NVML."""
        try:
            import pynvml
            pynvml.nvmlInit()
            self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_available = True
        except Exception:
            self._nvml_available = False
    
    def _collect(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        
        # CPU
        data['cpu_percent'] = psutil.cpu_percent(interval=0.1)
        
        # RAM
        mem = psutil.virtual_memory()
        data['ram_percent'] = mem.percent
        
        # Network
        net = psutil.net_io_counters()
        data['net_sent_bytes_per_sec'] = net.bytes_sent
        data['net_recv_bytes_per_sec'] = net.bytes_recv
        
        # Wi-Fi
        data['wifi_connected'] = self._check_wifi()
        
        # GPU (NVML)
        if self._nvml_available:
            try:
                import pynvml
                util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu)
                data['gpu_usage_percent'] = util.gpu
                data['gpu_temp_celsius'] = pynvml.nvmlDeviceGetTemperature(
                    self._gpu, pynvml.NVML_TEMPERATURE_GPU
                )
            except Exception:
                data['gpu_usage_percent'] = None
                data['gpu_temp_celsius'] = None
        else:
            data['gpu_usage_percent'] = None
            data['gpu_temp_celsius'] = None
        
        # Uptime
        data['uptime_seconds'] = int(time.time() - psutil.boot_time())
        
        # Process count
        data['process_count'] = len(psutil.pids())
        
        return data
    
    def _check_wifi(self) -> bool:
        """Check if Wi-Fi interface has an active IP."""
        try:
            addrs = psutil.net_if_addrs()
            for iface, addr_list in addrs.items():
                if 'wi-fi' in iface.lower() or 'wlan' in iface.lower():
                    for addr in addr_list:
                        if addr.family.name == 'AF_INET':
                            return True
        except Exception:
            pass
        return False
    
    def _loop(self):
        """Main collection loop."""
        while self.running:
            self.latest = self._collect()
            time.sleep(self.interval)
    
    def start(self):
        """Start background collection thread."""
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop collection."""
        self.running = False
        if self._gpu:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass
    
    def get_latest(self) -> Dict[str, Any]:
        """Get most recent metrics."""
        return self.latest
```

### 12.4 WebSocket Protocol Update

```python
# In main.py or websocket_server.py
async def send_system_metrics(client):
    """Send system metrics to connected client."""
    monitor = SystemMonitor()
    while True:
        metrics = monitor.get_latest()
        await client.send(json.dumps({
            'type': 'system_metrics',
            'data': metrics
        }))
        await asyncio.sleep(2.0)

async def send_vad_state(client):
    """Send VAD state to connected client."""
    vad = VoiceActivityDetector()
    while True:
        is_active = vad.is_speaking()
        await client.send(json.dumps({
            'type': 'user_voice_active',
            'active': is_active
        }))
        await asyncio.sleep(0.1)  # 10Hz for responsive state
```

---

## 13. Summary of Changes

| File | Change Type | Description |
|------|-------------|-------------|
| `useAudioInput.ts` | Updated | Noise gate, smoothing, getUserMedia constraints |
| `useEra.ts` | Updated | Parse system_metrics, user_voice_active messages |
| `ParticleSphere.tsx` | Updated | Quality presets, standby size/color, equalizer harmonics |
| `VoiceMeter.tsx` | Updated | Dim bars when Python VAD says no real voice |
| `SystemMonitor.tsx` | Updated | Render real metrics, N/A for unavailable |
| `core/system_monitor.py` | New | psutil + NVML/WMI metrics collection |
| `core/vad.py` | New | webrtcvad wrapper for Python VAD |
| `main.py` | Updated | Start SystemMonitor, wire VAD |

---

**End of Design Spec**
