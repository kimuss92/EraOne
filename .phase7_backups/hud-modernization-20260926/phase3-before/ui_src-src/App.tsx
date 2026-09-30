import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { FileUp, WifiOff } from 'lucide-react';
import HudHeader from './components/HudHeader';
import CoreStage from './components/CoreStage';
import ActivityPanel from './components/ActivityPanel';
import SystemPanel from './components/SystemPanel';
import ContextPanel, { MemoryPanel, WeatherPanel } from './components/ContextPanel';
import LiveTranscript from './components/LiveTranscript';
import CommandBar, { type CommandBarHandle } from './components/CommandBar';
import ConversationDrawer from './components/ConversationDrawer';
import DiagnosticsDrawer from './components/DiagnosticsDrawer';
import SettingsDrawer from './components/SettingsDrawer';
import CapabilitiesDrawer from './components/CapabilitiesDrawer';
import CommandPalette from './components/CommandPalette';
import { DOCK_QUERY, getState, setState, useHud, useMedia } from './lib/store';
import { actions, api, connect } from './lib/transport';
import { setSoundsEnabled } from './lib/sounds';
import { STATE_HUE } from './lib/format';
import type { HudSettings } from './lib/types';

// ERA ONE — the localhost HUD. Presentation only: every value on screen comes
// from the local backend (WebSocket events + REST), and every control calls a
// real backend operation. See app/hud/ for the other side of each contract.

interface Caps {
  count: number;
  skills: { id: string; count: number }[];
}

export default function App() {
  const link = useHud(s => s.link);
  const agent = useHud(s => s.agent);
  const theme = useHud(s => s.theme);
  const drawer = useHud(s => s.drawer);
  const palette = useHud(s => s.palette);
  const showLeft = useHud(s => s.showLeft);
  const showRight = useHud(s => s.showRight);
  const overlay = useHud(s => s.overlay);
  const dockLeft = useMedia(DOCK_QUERY.left);
  const dockRight = useMedia(DOCK_QUERY.right);
  const [caps, setCaps] = useState<Caps | null>(null);
  const [voice, setVoice] = useState<{ tts: string; stt: string } | null>(null);
  const [notice, setNotice] = useState('');
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const cmdRef = useRef<CommandBarHandle>(null);
  const memorySearchRef = useRef<HTMLInputElement>(null);
  const ptt = useRef(false);

  useEffect(() => { connect(); }, []);

  // Theme + panel layout persist per viewer.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('era_theme', theme); } catch { /* private mode */ }
  }, [theme]);
  useEffect(() => {
    try {
      localStorage.setItem('era_hud_left', showLeft ? '1' : '0');
      localStorage.setItem('era_hud_right', showRight ? '1' : '0');
    } catch { /* private mode */ }
  }, [showLeft, showRight]);

  // Registry-driven facts, refreshed whenever the link (re)opens.
  useEffect(() => {
    if (link !== 'online') return;
    api<Caps>('/api/capabilities').then(setCaps).catch(() => setCaps(null));
    api<HudSettings>('/api/settings').then(s => setSoundsEnabled(s.hud_sounds)).catch(() => {});
    const loadStatus = () =>
      api<{ voice: { tts: string; stt: string } }>('/api/status').then(s => setVoice(s.voice)).catch(() => setVoice(null));
    loadStatus();
    const t = window.setInterval(loadStatus, 30_000);
    return () => window.clearInterval(t);
  }, [link]);

  // Every real skill category gets a node (alphabetical = stable positions),
  // so whichever tool runs, its own node is the one that lights up.
  const nodes = (caps?.skills || [])
    .map(s => s.id)
    .filter(id => id !== 'safety')
    .sort();

  const flash = useCallback((msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(n => (n === msg ? '' : n)), 5000);
  }, []);

  const sendFiles = useCallback((files: File[]) => {
    if (getState().link !== 'online') return;
    files.forEach(f => actions.sendFile(f, flash));
    if (files.length) flash(`Sent ${files.map(f => f.name).join(', ')} to ERA ONE.`);
  }, [flash]);

  // Global keys: Ctrl/⌘+K palette · "/" focus · Esc close-or-interrupt ·
  // hold Space = push-to-talk while the mic is off.
  useEffect(() => {
    const typing = () => {
      const el = document.activeElement;
      return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement;
    };
    const down = (e: KeyboardEvent) => {
      const s = getState();
      if (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        setState({ palette: !s.palette });
      } else if (e.key === 'Escape') {
        if (s.palette) setState({ palette: false });
        else if (s.drawer) setState({ drawer: null });
        else if (s.overlay) setState({ overlay: null });
        else if (s.agent.state === 'speaking') actions.interrupt();
        else (document.activeElement as HTMLElement | null)?.blur();
      } else if (e.key === '/' && !typing() && !s.palette) {
        e.preventDefault();
        cmdRef.current?.focus();
      } else if (e.code === 'Space' && !typing() && !e.repeat && !s.palette && !s.drawer) {
        if (!s.micActive && !s.standby && s.link === 'online') {
          e.preventDefault();
          ptt.current = true;
          actions.setMic(true);
        }
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === 'Space' && ptt.current) {
        ptt.current = false;
        actions.setMic(false);
      }
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); };
  }, []);

  const state = link === 'online' ? agent.state : 'offline';
  const stateHue = STATE_HUE[state];
  // Docked rails follow the persisted toggles; below docking width a rail is
  // an on-demand overlay (never open by default, never both at once).
  const leftShown = dockLeft ? showLeft : overlay === 'left';
  const rightShown = dockRight ? showRight : overlay === 'right';

  return (
    <div
      className="hud-root relative flex h-screen w-screen flex-col overflow-hidden"
      style={{ '--sh': stateHue.h, '--ss': `${stateHue.s}%` } as CSSProperties}
      data-state={state}
      onContextMenu={e => { if (!(e.target as HTMLElement).closest('.select-text')) e.preventDefault(); }}
      onDragEnter={e => { e.preventDefault(); dragDepth.current += 1; setDragging(true); }}
      onDragLeave={e => { e.preventDefault(); dragDepth.current -= 1; if (dragDepth.current <= 0) { dragDepth.current = 0; setDragging(false); } }}
      onDragOver={e => e.preventDefault()}
      onDrop={e => { e.preventDefault(); dragDepth.current = 0; setDragging(false); sendFiles(Array.from(e.dataTransfer.files)); }}
    >
      <div className="hud-grid pointer-events-none absolute inset-0" />
      <div className="hud-vignette pointer-events-none absolute inset-0" />

      <HudHeader toolCount={caps?.count ?? null} voice={voice} />

      {link !== 'online' && (
        <div className="banner absolute left-1/2 top-16 z-[var(--z-banner)] -translate-x-1/2" role="status">
          <WifiOff className="h-3.5 w-3.5" />
          {link === 'connecting' ? 'CONNECTING TO ERA ONE…' : 'RECONNECTING TO ERA ONE…'}
          <span className="t2 normal-case tracking-normal">the backend is not reachable on this machine</span>
        </div>
      )}

      <main className="relative z-[var(--z-content)] flex min-h-0 flex-1 gap-3 p-3">
        {leftShown && (
          <aside className={`rail rail-left ${dockLeft ? '' : 'rail-overlay'}`}>
            <ActivityPanel />
            <SystemPanel />
          </aside>
        )}

        <section
          className="relative flex min-w-0 flex-1 flex-col"
          onPointerDown={() => { if (getState().overlay) setState({ overlay: null }); }}
        >
          <CoreStage nodes={nodes} />
          <div className="relative z-[var(--z-stage)] shrink-0 space-y-3 pt-2">
            <LiveTranscript />
            {notice && <p className="text-center text-meta t2" role="status">{notice}</p>}
            <CommandBar ref={cmdRef} onFiles={sendFiles} />
          </div>
        </section>

        {rightShown && (
          <aside className={`rail rail-right ${dockRight ? '' : 'rail-overlay'}`}>
            <ContextPanel />
            <MemoryPanel searchRef={memorySearchRef} />
            <WeatherPanel />
          </aside>
        )}
      </main>

      {dragging && (
        <div className="absolute inset-0 z-[var(--z-drop)] flex items-center justify-center bg-[var(--veil)] backdrop-blur-sm">
          <div className="drop-target flex flex-col items-center gap-3 p-10">
            <FileUp className="h-10 w-10 ac-text" />
            <p className="text-body t1">Drop files to send them to ERA ONE</p>
          </div>
        </div>
      )}

      {drawer === 'conversation' && <ConversationDrawer />}
      {drawer === 'diagnostics' && <DiagnosticsDrawer />}
      {drawer === 'settings' && <SettingsDrawer />}
      {drawer === 'capabilities' && <CapabilitiesDrawer />}
      {palette && (
        <CommandPalette
          focusInput={() => cmdRef.current?.focus()}
          focusMemorySearch={() => memorySearchRef.current?.focus()}
        />
      )}
    </div>
  );
}
