import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { FileUp, WifiOff } from 'lucide-react';
import HudHeader from './components/HudHeader';
import CoreStage from './components/CoreStage';
import VisualWorkspace from './components/VisualWorkspace';
import DevelopmentDrawer from './components/DevelopmentDrawer';
import { DevelopmentProvider } from './lib/development';
import ActivityPanel from './components/ActivityPanel';
import SystemPanel from './components/SystemPanel';
import ContextPanel, { MemoryPanel, SourcesPanel, WeatherPanel } from './components/ContextPanel';
import LiveTranscript from './components/LiveTranscript';
import ApprovalPrompt from './components/ApprovalPrompt';
import CommandBar, { type CommandBarHandle } from './components/CommandBar';
import ConversationDrawer from './components/ConversationDrawer';
import DiagnosticsDrawer from './components/DiagnosticsDrawer';
import SettingsDrawer from './components/SettingsDrawer';
import CapabilitiesDrawer from './components/CapabilitiesDrawer';
import CommandPalette from './components/CommandPalette';
import NotificationRail from './components/NotificationRail';
import { notify } from './lib/notifications';
import ScheduledPanel from './components/ScheduledPanel';
import ScheduleDrawer from './components/ScheduleDrawer';
import { useScheduleSync } from './lib/schedule';
import ShortcutsDrawer from './components/ShortcutsDrawer';
import { DOCK_QUERY, getState, setState, togglePanel, useHud, useMedia } from './lib/store';
import { matchShortcut, type ShortcutId } from './lib/shortcuts';
import { actions, api, connect } from './lib/transport';
import { setSoundsEnabled } from './lib/sounds';
import { STATE_HUE } from './lib/format';
import type { DrawerId, HudSettings } from './lib/types';

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
  const approval = useHud(s => s.approval);
  const overlay = useHud(s => s.overlay);
  const dockLeft = useMedia(DOCK_QUERY.left);
  const dockRight = useMedia(DOCK_QUERY.right);
  const [caps, setCaps] = useState<Caps | null>(null);
  const [voice, setVoice] = useState<{ tts: string; stt: string } | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const cmdRef = useRef<CommandBarHandle>(null);
  const memorySearchRef = useRef<HTMLInputElement>(null);
  const ptt = useRef(false);

  useEffect(() => { connect(); }, []);
  useScheduleSync();

  // High-risk approvals live with the activity feed, never over the central sphere.
  // Open the left rail while an approval is visible so its action buttons are reachable.
  useEffect(() => {
    if (!approval || link !== 'online') return;
    if (dockLeft) {
      if (!showLeft) setState({ showLeft: true });
    } else if (overlay !== 'left') {
      setState({ overlay: 'left' });
    }
  }, [approval?.id, link, dockLeft, showLeft, overlay]);

  // "+ Schedule": pre-fill the command bar only — nothing is sent until Enter.
  const startSchedule = useCallback(() => {
    setState({ drawer: null, palette: false, overlay: null });
    cmdRef.current?.setText('Schedule ');
  }, []);

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
    api<{ events: Array<{ task_id: string; seq: number; ts: number; status: string; goal: string; message: string; error_type?: string }> }>(
      '/api/tasks/events?limit=100',
    ).then(({ events }) => {
      const restored = events.map(event => ({
        id: `task-${event.task_id}-${event.seq}`,
        ts: event.ts,
        kind: 'task',
        label: `Task ${event.status} · ${event.goal.slice(0, 120)}`,
        detail: event.message,
        tone: event.status === 'completed' ? 'ok' as const : event.status === 'failed' ? 'bad' as const : event.status === 'cancelled' ? 'warn' as const : 'info' as const,
      }));
      setState(s => {
        const byId = new Map(s.timeline.map(item => [item.id, item]));
        restored.forEach(item => byId.set(item.id, item));
        return { timeline: [...byId.values()].sort((a, b) => a.ts - b.ts).slice(-40) };
      });
    }).catch(() => {});
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

  // HUD-side feedback (file sends) goes to the notification rail as info.
  const flash = useCallback((msg: string) => {
    notify({ kind: 'info', title: msg });
  }, []);

  const sendFiles = useCallback((files: File[]) => {
    if (getState().link !== 'online') return;
    files.forEach(f => actions.sendFile(f, flash));
    if (files.length) flash(`Sent ${files.map(f => f.name).join(', ')} to ERA ONE.`);
  }, [flash]);

  // Global keys: Ctrl/⌘+K palette · "/" focus · Esc close-or-interrupt ·
  // hold Space = push-to-talk while the mic is off · single-key shortcuts
  // (lib/shortcuts.ts) when not typing.
  useEffect(() => {
    const typing = () => {
      const el = document.activeElement;
      return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
        || el instanceof HTMLSelectElement || (el instanceof HTMLElement && el.isContentEditable);
    };
    const toggleDrawer = (id: DrawerId) => setState(s => ({ drawer: s.drawer === id ? null : id }));
    const runShortcut = (id: ShortcutId) => {
      const s = getState();
      switch (id) {
        case 'mic': if (s.link === 'online') actions.setMic(!s.micActive); break;
        case 'standby': if (s.link === 'online') actions.setStandby(!s.standby); break;
        case 'ask': cmdRef.current?.focus(); break;
        case 'conversation': toggleDrawer('conversation'); break;
        case 'schedule': toggleDrawer('schedule'); break;
        case 'capabilities': toggleDrawer('capabilities'); break;
        case 'diagnostics': toggleDrawer('diagnostics'); break;
        case 'settings': toggleDrawer('settings'); break;
        case 'help': toggleDrawer('shortcuts'); break;
        case 'left': togglePanel('left'); break;
        case 'right': togglePanel('right'); break;
        case 'fullscreen':
          if (document.fullscreenElement) void document.exitFullscreen();
          else void document.documentElement.requestFullscreen?.().catch(() => {});
          break;
        // Destructive: the key only opens the palette on its confirm step.
        case 'restart': setState({ palette: true, paletteConfirm: 'restart' }); break;
        case 'exit': setState({ palette: true, paletteConfirm: 'shutdown' }); break;
      }
    };
    const down = (e: KeyboardEvent) => {
      const s = getState();
      const sc = !typing() && !s.palette && !e.repeat ? matchShortcut(e) : null;
      if (sc) {
        e.preventDefault();
        runShortcut(sc);
        return;
      }
      if (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        setState({ palette: !s.palette });
      } else if (e.key === 'Escape') {
        if (s.palette) setState({ palette: false });
        else if (s.drawer) setState({ drawer: null });
        else if (s.overlay) setState({ overlay: null });
        else if (s.agent.state === 'speaking') actions.interrupt();
        else (document.activeElement as HTMLElement | null)?.blur();
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
    <DevelopmentProvider><div
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
      <NotificationRail />

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
            <ApprovalPrompt />
            <ActivityPanel />
            <ScheduledPanel onSchedule={startSchedule} />
            <SystemPanel />
          </aside>
        )}

        <section
          className="relative flex min-w-0 flex-1 flex-col"
          onPointerDown={() => { if (getState().overlay) setState({ overlay: null }); }}
        >
          <VisualWorkspace />
          <CoreStage nodes={nodes} />
          <div className="relative z-[var(--z-stage)] shrink-0 space-y-3 pt-2">
            <LiveTranscript />
            <CommandBar ref={cmdRef} onFiles={sendFiles} />
          </div>
        </section>

        {rightShown && (
          <aside className={`rail rail-right ${dockRight ? '' : 'rail-overlay'}`}>
            <ContextPanel />
            <SourcesPanel />
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
      {drawer === 'schedule' && <ScheduleDrawer onSchedule={startSchedule} />}
      {drawer === 'shortcuts' && <ShortcutsDrawer />}
      {drawer === 'development' && <DevelopmentDrawer />}
      {palette && (
        <CommandPalette
          focusInput={() => cmdRef.current?.focus()}
          focusMemorySearch={() => memorySearchRef.current?.focus()}
          startSchedule={startSchedule}
        />
      )}
    </div></DevelopmentProvider>
  );
}
