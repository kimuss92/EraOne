import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Mic, MicOff, Send, Power, FileUp } from 'lucide-react';
import NeuralCore, { type QualityPreset } from './components/NeuralCore';
import SystemMonitor from './components/SystemMonitor';
import GeoWeather from './components/GeoWeather';
import TitleBar from './components/TitleBar';
import ThinkingIndicator from './components/ThinkingIndicator';
import VoiceMeter from './components/VoiceMeter';
import LogPanel from './components/LogPanel';
import HudOverlay from './components/HudOverlay';
import ModelIndicator from './components/ModelIndicator';
import { useAudioInput } from './hooks/useAudioInput';
import { useEra } from './hooks/useEra';

// RENDER ARCHITECTURE — the old App kept a 150ms clock, a 1s uptime counter
// and per-frame mic volume in root state, re-rendering the entire tree up to
// monitor refresh rate ("the glitch"). Now:
//   * clock lives inside TitleBar, uptime inside SystemMonitor
//   * Era voice amplitude is a ref, read by the sphere's own rAF loop
//   * mic volume state is throttled to ~12Hz and quantized in useAudioInput
//   * every heavy child is React.memo'd, so those 12Hz ticks only touch
//     the VoiceMeter
// Nothing here should ever re-render faster than ~12Hz.
//
// EMOTION ACCENT — Era's mood tints the whole interface through two CSS
// custom properties (--ah hue, --as saturation) set once on the root div.
// Every ac-* utility (index.css) derives from them and carries its own CSS
// transition, so a mood change recolors everything with ZERO extra
// re-renders. The sphere gets the same pair as props and lerps internally.

// Cyan-family HUD palette (the "dark futuristic OS" redesign). Each assistant
// state owns a hue; offline and error are derived states layered on top of
// activity (offline = WS link down, error = a recent ERR/WARN system log).
const THEMES: Record<string, { h: number; s: number }> = {
  idle: { h: 195, s: 80 },       // cool cyan — online / ready
  listening: { h: 187, s: 95 },  // electric cyan — receptive
  thinking: { h: 45, s: 92 },    // golden — reasoning (explicit user direction)
  processing: { h: 275, s: 85 }, // violet-magenta — heavy computation
  speaking: { h: 172, s: 85 },   // teal — voicing
  standby: { h: 355, s: 80 },    // red — asleep (explicit user direction)
  offline: { h: 212, s: 14 },    // desaturated steel — link down
  error: { h: 32, s: 95 },       // amber — controlled warning, distinct from standby red
};

const ERROR_FLASH_MS = 4000;

const HISTORY_KEY = 'era_cmd_history';
const HISTORY_MAX = 50;

// ── THEME (dark default / light) ─────────────────────────────────────────
// A single data-theme attribute on <html> flips every token in index.css —
// no React re-render is needed for the repaint, only for the toggle icon.
// Sources of truth for changes: the title-bar button, the
// window.__era_set_theme bridge (Python runJavaScript), and the WS
// {type:'theme'} message (voice command), which useEra re-emits as an
// 'era:theme' CustomEvent.
type ThemeMode = 'dark' | 'light';
const THEME_KEY = 'era_theme';

function loadTheme(): ThemeMode {
  return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark';
}

// ── ACTIVE-MODEL PLACEMENT ───────────────────────────────────────────────
// The model pill sits in the HEADER, centred directly under the title block.
//
// It previously floated at the shell's bottom-left, sharing a baseline with the
// rails. That worked, but it put the one status you most need to read in the
// corner furthest from where you look, and it had to reserve padding inside the
// left rail's scroll area so it never covered the weather rows. Here it needs
// no reservation at all: it is in FLOW inside the centre column's header, so
// nothing overlaps and nothing has to make room.
//
// It centres on the CORE's axis rather than the window, because it lives inside
// the same `flex-1 min-w-0` column as the title and the command pill. That is
// what keeps all three on one vertical line at any window width — centring on
// the window instead is the bug the <main> comment below documents.

function loadHistory(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.filter(v => typeof v === 'string') : [];
  } catch {
    return [];
  }
}

export default function App() {
  const { connected, activity, muted, logs, activeModel, modelCatalog, selectedModel, maximized, browserMicActive, metrics, userVoiceActive, agentAmplitudeRef, agentBandsRef, sendCommand, sendFile, toggleMute, sendWindowControl, sendMicToggle, sendMicVolume, selectModel } = useEra();

  const [input, setInput] = useState('');
  const [dragging, setDragging] = useState(false);
  const [lastDroppedFiles, setLastDroppedFiles] = useState<string[]>([]);
  const [themeMode, setThemeMode] = useState<ThemeMode>(loadTheme);

  // Apply + persist theme; expose the voice/backend bridges.
  useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
    try { localStorage.setItem(THEME_KEY, themeMode); } catch {}
  }, [themeMode]);

  useEffect(() => {
    const apply = (mode: string) => {
      if (mode === 'toggle') setThemeMode(m => (m === 'dark' ? 'light' : 'dark'));
      else if (mode === 'light' || mode === 'dark') setThemeMode(mode);
    };
    (window as any).__era_set_theme = apply;
    const onWsTheme = (e: Event) => apply(String((e as CustomEvent).detail));
    window.addEventListener('era:theme', onWsTheme);
    return () => {
      delete (window as any).__era_set_theme;
      window.removeEventListener('era:theme', onWsTheme);
    };
  }, []);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const dragCounterRef = useRef(0);

  // Command history (↑/↓ recall, persisted across sessions)
  const historyRef = useRef<string[]>(loadHistory());
  const historyPosRef = useRef(-1); // -1 = live input
  const draftRef = useRef('');

  const isStandby = muted || activity === 'standby';

  // Error accent flash — an ERR/WARN system log tints the whole interface
  // amber for a few seconds, then it settles back to the activity color.
  const [errorFlash, setErrorFlash] = useState(false);
  useEffect(() => {
    const last = logs[logs.length - 1];
    if (last && last.role === 'system' && /^(ERR|WARN|SAFETY)/i.test(last.text)) {
      setErrorFlash(true);
      const t = window.setTimeout(() => setErrorFlash(false), ERROR_FLASH_MS);
      return () => window.clearTimeout(t);
    }
  }, [logs]);

  const accentKey = !connected ? 'offline'
    : errorFlash ? 'error'
    : isStandby ? 'standby'
    : activity;
  const theme = THEMES[accentKey] ?? THEMES.idle;

  const sendMessage = (text?: string) => {
    const msg = (text ?? input).trim();
    if (!msg) return;
    setInput('');
    sendCommand(msg);
    // Record in history (skip consecutive duplicates)
    const h = historyRef.current;
    if (h[h.length - 1] !== msg) {
      h.push(msg);
      if (h.length > HISTORY_MAX) h.splice(0, h.length - HISTORY_MAX);
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); } catch {}
    }
    historyPosRef.current = -1;
    draftRef.current = '';
  };

  const recallHistory = (dir: -1 | 1) => {
    const h = historyRef.current;
    if (!h.length) return;
    let pos = historyPosRef.current;
    if (dir === -1) {
      if (pos === -1) { draftRef.current = input; pos = h.length - 1; }
      else if (pos > 0) pos -= 1;
      else return;
    } else {
      if (pos === -1) return;
      pos = pos < h.length - 1 ? pos + 1 : -1;
    }
    historyPosRef.current = pos;
    setInput(pos === -1 ? draftRef.current : h[pos]);
  };

  const { micActive, volume, peakVolume, start, stop } = useAudioInput();

  // Send mic volume to Python backend for VAD processing
  useEffect(() => {
    if (!micActive) return;
    const interval = setInterval(() => {
      sendMicVolume(volume);
    }, 100); // 10Hz update rate
    return () => clearInterval(interval);
  }, [micActive, volume, sendMicVolume]);

  // ── Mic single-source-of-truth sync ─────────────────────────────────────
  // `browserMicActive` (the backend's echoed state) is the ONLY authority on
  // whether the mic is on. The local meter mic (`micActive`, getUserMedia for
  // the volume bars) FOLLOWS it — it never leads. Previously the indicator
  // showed the local mic while the backend gated audio on its own flag; one
  // missed message (or an F1 press, which only flipped the local mic) inverted
  // them permanently — the "mic OFF but Era hears me / mic ON but deaf" bug.
  useEffect(() => {
    if (browserMicActive && !micActive) start();
    else if (!browserMicActive && micActive) stop();
  }, [browserMicActive, micActive, start, stop]);

  // F1 mic toggle bridge (called from Python via runJavaScript).
  // Routes through the backend like the button does; the local meter mic
  // follows the echoed state via the sync effect above.
  useEffect(() => {
    (window as any).__era_toggle_mic = () => {
      sendMicToggle(!browserMicActive);
    };
    return () => { delete (window as any).__era_toggle_mic; };
  }, [browserMicActive, sendMicToggle]);

  // "/" or Ctrl+K anywhere focuses the command input (launcher-style)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      const typing = el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement;
      if ((e.key === '/' && !typing) || (e.key.toLowerCase() === 'k' && e.ctrlKey)) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const sphereQuality: QualityPreset = useMemo(() => {
    if (isStandby) return 'low';
    if (activity === 'speaking') return 'high';
    return 'medium';
  }, [isStandby, activity]);

  const dropHandlers = {
    onDragEnter: (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounterRef.current += 1;
      setDragging(true);
    },
    onDragLeave: (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounterRef.current -= 1;
      if (dragCounterRef.current <= 0) {
        dragCounterRef.current = 0;
        setDragging(false);
      }
    },
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (isStandby) {
        dragCounterRef.current = 0;
        setDragging(false);
        return;
      }
      dragCounterRef.current = 0;
      setDragging(false);
      const files = Array.from(e.dataTransfer.files);
      if (!files.length) return;
      setLastDroppedFiles(files.map(f => f.name));
      files.forEach(file => sendFile(file));
    },
  };

  const onFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    files.forEach(file => sendFile(file));
    setLastDroppedFiles(files.map(f => f.name));
    e.currentTarget.value = '';
  };

  const stateLabel = !connected ? 'OFFLINE'
    : errorFlash ? 'WARNING'
    : isStandby ? 'STANDBY'
    : !browserMicActive ? 'MIC OFF'
    : activity === 'listening' ? 'LISTENING'
    : activity === 'speaking' ? 'SPEAKING'
    : activity === 'thinking' ? 'THINKING'
    : activity === 'processing' ? 'PROCESSING'
    : 'IDLE';

  return (
    <div
      ref={rootRef}
      {...dropHandlers}
      onContextMenu={(e) => e.preventDefault()}
      style={{ '--ah': theme.h, '--as': `${theme.s}%` } as CSSProperties}
      className="relative z-0 flex h-screen w-screen flex-col overflow-hidden select-none"
    >
      {/* ── Title bar ──────────────────────────────────────────── */}
      <TitleBar
        maximized={maximized}
        onWindowControl={sendWindowControl}
        themeMode={themeMode}
        onToggleTheme={() => setThemeMode(m => (m === 'dark' ? 'light' : 'dark'))}
      />

      {/* Invisible click-capture — prevents click-through to Qt drag region */}
      <div className="absolute inset-0 z-[1]" />

      {/* Background grid / vignette — token-driven so both themes vignette
          toward their own background instead of always toward black */}
      <div
        className="pointer-events-none absolute inset-0 z-0"
        style={{ background: 'radial-gradient(circle at center, transparent 0%, var(--veil) 100%)' }}
      />
      <div
        className="pointer-events-none absolute inset-0 z-0 opacity-20"
        style={{
          backgroundImage:
            'linear-gradient(var(--grid-line) 1px, transparent 1px), linear-gradient(90deg, var(--grid-line) 1px, transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      {/* Horizon sweep — single compositor-only animation, transform/opacity */}
      {!isStandby && <div className="scan-sweep pointer-events-none absolute inset-x-0 top-10 z-0 h-px" />}

      {/* Drag overlay */}
      {dragging && (
        <div className="absolute inset-0 z-50 flex items-center justify-center backdrop-blur-sm" style={{ background: 'var(--veil)' }}>
          <div className="ac-border ac-bg-soft ac-text ac-glow flex flex-col items-center gap-4 rounded-3xl border p-10">
            <FileUp className="h-12 w-12" />
            <p className="text-lg font-medium tracking-wide">Drop files anywhere to load into ERA ONE</p>
          </div>
        </div>
      )}

      {/* Hidden file input */}
      <input ref={fileInputRef} type="file" multiple className="hidden" onChange={onFileSelect} />

      {/* ── BODY: three real flow columns ────────────────────────────────
          The panels used to be absolutely positioned overlays while the core,
          the HUD and the command bar were anchored to the WINDOW's centre. That
          layout has no idea the panels exist: shrink the window and the centre
          column keeps sitting where it always sat, so the sphere slid under the
          conversation panel and the command pill covered the clock.

          Now the left rail, the centre stack and the conversation panel are
          siblings in a flex row. The centre is `flex-1 min-w-0`, so it is BY
          CONSTRUCTION exactly the space the two rails don't occupy, and the
          things centred inside it (core, HUD, orbit rings, command pill) centre
          on that free band rather than on the window. Nothing can overlap
          anything, at any width, because nothing is floating over anything. */}
      <main className="relative z-10 flex min-h-0 flex-1 items-stretch gap-4 p-4">
        {/* Left rail — ONE sheet of glass (reference design), not a stack of
            separate cards: header with title + link badge + power, then the
            system gauges, then the weather section, separated by hairline
            dividers. Scrolls internally on short windows. */}
        {/* Same width and full height as the conversation panel opposite —
            the two rails are visual mirrors (reference layout). */}
        <aside className="pointer-events-auto z-20 flex w-60 shrink-0 flex-col overflow-hidden rounded-3xl glass xl:w-72">
          <div className="flex items-center justify-between border-b border-line-soft px-4 py-3">
            <span className="flex items-center gap-2">
              <span className={`ac-dot h-1.5 w-1.5 rounded-full ${
                activity === 'thinking' || activity === 'processing' ? 'animate-pulse' : ''
              }`} />
              <span className="ac-text text-[12px] font-semibold tracking-wide">System Status</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span
                className={`rounded-full px-2 py-0.5 text-[9px] font-bold tracking-wide ${
                  !connected ? 'bg-amber-400/15 text-amber-400'
                  : isStandby ? 'bg-red-400/15 text-red-400'
                  : 'bg-emerald-400/15 text-emerald-400'
                }`}
              >
                {stateLabel}
              </span>
              <button
                onClick={() => toggleMute()}
                title="Toggle standby"
                className="ac-chip flex h-6 w-6 items-center justify-center rounded-full"
              >
                <Power className="h-3 w-3" />
              </button>
            </span>
          </div>
          <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto">
            <SystemMonitor muted={muted} metrics={metrics} />
            <GeoWeather />
          </div>
        </aside>

        {/* Centre stack: title · core (absolute, fills this column) · command bar.
            `@container` makes THIS COLUMN the query target for everything inside
            it. Viewport breakpoints (xl:) are the wrong question here — a 1600px
            window with both rails open still leaves a narrow band, and gating on
            the window is what let the HUD tags land on top of the sphere. The
            children below ask how much room the COLUMN has, which is the only
            width that decides whether they fit. */}
        <section className="@container relative flex min-w-0 flex-1 flex-col">
          <NeuralCore
            mode={isStandby ? 'standby' : 'active'}
            activity={activity}
            amplitudeRef={agentAmplitudeRef}
            bandsRef={agentBandsRef}
            hue={theme.h}
            sat={theme.s}
            quality={sphereQuality}
            light={themeMode === 'light'}
            offline={!connected}
          />

          {/* Floating HUD chrome: micro-labels, sparkline, connector, floor glow */}
          <HudOverlay
            metrics={metrics}
            activity={activity}
            connected={connected}
            standby={isStandby}
          />

          {/* Orbit rings around the core — two compositor-only rotations.
              Sized at 2.24 × --R (the shared sphere radius from .core-geom,
              see index.css), so the rings ride the sphere at BOTH size tiers
              instead of restating its formula and drifting out of step. */}
          <div className="core-geom pointer-events-none absolute left-1/2 top-1/2 z-0 h-[calc(var(--R)*2.24)] w-[calc(var(--R)*2.24)] -translate-x-1/2 -translate-y-1/2">
            <div className="orbit-ring absolute inset-0 rounded-full" />
            <div className="orbit-ring absolute -inset-[26px] rounded-full opacity-50" style={{ animationDuration: '150s', animationDirection: 'reverse' }} />
            <div className="orbit-carrier absolute inset-0">
              <div className="orbit-sat absolute left-1/2 top-0 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full" />
            </div>
          </div>

          {/* Thinking / Processing indicator */}
          {(activity === 'thinking' || activity === 'processing') && (
            <ThinkingIndicator mode={activity} />
          )}

          <header className="relative z-10 flex shrink-0 items-center justify-center pt-2">
            <div className="text-center">
              <h1 className="ac-text-strong ac-glow-text text-2xl font-bold tracking-[0.35em] xl:text-3xl">
                ERA ONE
              </h1>
              <p className="ac-label mt-1 text-[10px] font-semibold tracking-[0.25em] uppercase">
                CTRLZ INTELLIGENCE SYSTEM
              </p>

              {/* Active model — centred under the title, on the core's axis.
                  Also the switcher: the chip that reports the model is the one
                  you click to change it, so there is no second control to fall
                  out of sync with this one. */}
              <div className="mt-3 flex justify-center">
                <ModelIndicator
                  model={activeModel}
                  connected={connected}
                  catalog={modelCatalog}
                  selected={selectedModel}
                  onSelect={selectModel}
                />
              </div>
            </div>
          </header>

          {/* The core owns this gap — it is the only thing between the title and
              the command bar, so the sphere can never be covered by either. */}
          <div className="min-h-0 flex-1" />

          {/* Command bar — ONE pill on the core's axis: mic, live voice meter,
              input, send. In FLOW at the bottom of the centre column, so it can
              never drift over the left rail the way the old fixed bar did. */}
          <div className="pointer-events-none relative z-30 flex shrink-0 flex-col items-center gap-2.5 pt-3">
            <div className="flex w-full items-end justify-center gap-4">
              {/* Text input */}
              <form
            onSubmit={(e) => {
              e.preventDefault();
              sendMessage();
            }}
            className="pointer-events-auto flex w-full max-w-xl items-end gap-2 rounded-[28px] glass p-2"
          >
            <button
              type="button"
              disabled={isStandby}
              onClick={() => {
                // Ask the backend for the opposite of ITS current state. The
                // local meter mic follows the echoed browser_mic message — the
                // button never flips local state directly, so UI and backend
                // can no longer hold opposite states.
                sendMicToggle(!browserMicActive);
              }}
              className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition disabled:opacity-40 @[560px]:h-11 @[560px]:w-11 ${
                browserMicActive
                  ? 'ac-bg-soft ac-text'
                  : 'bg-red-500/20 text-red-400 hover:bg-red-500/30'
              }`}
            >
              {browserMicActive ? <Mic className="h-5 w-5" /> : <MicOff className="h-5 w-5" />}
            </button>

            {/* Live voice meter, inline: it belongs next to the mic that feeds
                it, not in a separate card across the screen. It is the first
                thing to go when the column gets tight — the meter is ambient,
                the thing you type into is not, and squeezing both left the
                textarea a scrollbar's width wide. */}
            <div className="hidden @[560px]:flex">
              <VoiceMeter
                volume={volume}
                peakVolume={peakVolume}
                micActive={browserMicActive}
                userVoiceActive={userVoiceActive}
                compact
              />
            </div>

            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                historyPosRef.current = -1;
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                } else if (e.key === 'ArrowUp' && (input === '' || historyPosRef.current !== -1)) {
                  e.preventDefault();
                  recallHistory(-1);
                } else if (e.key === 'ArrowDown' && historyPosRef.current !== -1) {
                  e.preventDefault();
                  recallHistory(1);
                } else if (e.key === 'Escape') {
                  setInput('');
                  historyPosRef.current = -1;
                }
              }}
              placeholder={isStandby ? 'Standby…' : 'Command ERA ONE…'}
              /* The "/ to focus · ↑ history" hint lives in the tooltip now: as a
                 placeholder it was long enough to wrap onto three lines once the
                 column narrowed, which forced a scrollbar into a 44px-tall field. */
              title="/ or Ctrl+K to focus · ↑ / ↓ for history"
              disabled={isStandby}
              /* min-w-0: without it the textarea keeps its default intrinsic
                 width and refuses to shrink in the flex row, so a tight pill
                 squeezed it to a sliver with a scrollbar instead of letting it
                 narrow gracefully. */
              className="bg-field t1 max-h-32 min-h-[36px] min-w-0 flex-1 resize-none rounded-[20px] px-3 py-2 text-xs placeholder:[color:var(--text-3)] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ah)_var(--as)_var(--al-border)/0.5)] disabled:opacity-50 @[560px]:min-h-[44px] @[560px]:px-4 @[560px]:py-2.5 @[560px]:text-sm"
            />
            <button
              type="submit"
              disabled={isStandby || !input.trim()}
              className="ac-chip flex h-9 w-9 shrink-0 items-center justify-center rounded-full disabled:opacity-40 @[560px]:h-11 @[560px]:w-11"
            >
              <Send className="h-5 w-5" />
            </button>
              </form>
            </div>
          </div>
        </section>

        {/* Conversation logs — a flow column, not an overlay */}
        <LogPanel logs={logs} lastDroppedFiles={lastDroppedFiles} />
      </main>

    </div>
  );
}
