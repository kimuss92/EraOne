import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Brain,
  CalendarClock,
  CalendarPlus,
  Gauge,
  Keyboard,
  Layers,
  Maximize,
  MessageSquareText,
  Mic,
  MicOff,
  Moon,
  PanelLeft,
  PanelRight,
  Power,
  RotateCcw,
  Search,
  Settings,
  Square,
  Sun,
  Terminal,
} from 'lucide-react';
import { DOCK_QUERY, getState, setState, togglePanel, useHud } from '../lib/store';
import { keyFor, type ShortcutId } from '../lib/shortcuts';
import { actions } from '../lib/transport';

interface Cmd {
  id: string;
  label: string;
  hint?: string;
  /** Shortcut id → the key chip on the right of the row. */
  sc?: ShortcutId;
  Icon: typeof Mic;
  run: () => void | Promise<unknown>;
  confirm?: string;
}

interface Props {
  focusInput: () => void;
  focusMemorySearch: () => void;
  startSchedule: () => void;
}

export default function CommandPalette({ focusInput, focusMemorySearch, startSchedule }: Props) {
  const micActive = useHud(s => s.micActive);
  const standby = useHud(s => s.standby);
  const theme = useHud(s => s.theme);
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const [confirming, setConfirming] = useState<Cmd | null>(null);
  const [note, setNote] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const close = () => setState({ palette: false });

  const cmds: Cmd[] = useMemo(() => [
    { id: 'ask', sc: 'ask', label: 'Ask ERA ONE…', hint: 'Focus the command bar', Icon: Terminal, run: () => { close(); focusInput(); } },
    micActive
      ? { id: 'mic', sc: 'mic', label: 'Mute microphone', Icon: MicOff, run: () => { actions.setMic(false); close(); } }
      : { id: 'mic', sc: 'mic', label: 'Start listening', hint: 'Turn the microphone on', Icon: Mic, run: () => { actions.setMic(true); close(); } },
    { id: 'stop', label: 'Stop speaking', hint: 'Esc', Icon: Square, run: () => { actions.interrupt(); close(); } },
    standby
      ? { id: 'standby', sc: 'standby', label: 'Wake ERA ONE', Icon: Power, run: () => { actions.setStandby(false); close(); } }
      : { id: 'standby', sc: 'standby', label: 'Enter standby', hint: 'Sleep until the wake word', Icon: Power, run: () => { actions.setStandby(true); close(); } },
    { id: 'conv', sc: 'conversation', label: 'Open conversation', Icon: MessageSquareText, run: () => setState({ palette: false, drawer: 'conversation' }) },
    { id: 'memory', label: 'Search memory…', Icon: Brain, run: () => { close(); if (window.matchMedia(DOCK_QUERY.right).matches) setState({ showRight: true }); else setState({ overlay: 'right' }); window.setTimeout(focusMemorySearch, 50); } },
    { id: 'sched', sc: 'schedule', label: 'Scheduled tasks', hint: 'Upcoming and waiting tasks', Icon: CalendarClock, run: () => setState({ palette: false, drawer: 'schedule' }) },
    { id: 'sched-new', label: 'Schedule a task…', hint: 'Pre-fills the command bar', Icon: CalendarPlus, run: () => startSchedule() },
    { id: 'caps', sc: 'capabilities', label: 'Open capabilities', Icon: Layers, run: () => setState({ palette: false, drawer: 'capabilities' }) },
    { id: 'activity', sc: 'left', label: 'Toggle live activity panel', Icon: PanelLeft, run: () => { close(); togglePanel('left'); } },
    { id: 'context', sc: 'right', label: 'Toggle context panel', Icon: PanelRight, run: () => { close(); togglePanel('right'); } },
    { id: 'diag', sc: 'diagnostics', label: 'Open diagnostics', Icon: Gauge, run: () => setState({ palette: false, drawer: 'diagnostics' }) },
    { id: 'settings', sc: 'settings', label: 'Settings', Icon: Settings, run: () => setState({ palette: false, drawer: 'settings' }) },
    { id: 'fs', sc: 'fullscreen', label: 'Toggle fullscreen', Icon: Maximize, run: () => { close(); if (document.fullscreenElement) void document.exitFullscreen(); else void document.documentElement.requestFullscreen?.().catch(() => {}); } },
    { id: 'keys', sc: 'help', label: 'Keyboard shortcuts', Icon: Keyboard, run: () => setState({ palette: false, drawer: 'shortcuts' }) },
    { id: 'theme', label: theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme', Icon: theme === 'dark' ? Sun : Moon, run: () => setState(s => ({ palette: false, theme: s.theme === 'dark' ? 'light' : 'dark' })) },
    { id: 'restart', sc: 'restart', label: 'Restart ERA ONE', hint: 'The HUD reconnects by itself', Icon: RotateCcw, confirm: 'Restart ERA ONE now?', run: () => actions.restart() },
    { id: 'shutdown', sc: 'exit', label: 'Shutdown ERA ONE', hint: 'Stops the backend; double-click ERA ONE to start again', Icon: Activity, confirm: 'Shut ERA ONE down completely?', run: () => actions.shutdown() },
  ], [micActive, standby, theme, focusInput, focusMemorySearch, startSchedule]);

  const shown = useMemo(() => {
    const n = q.trim().toLowerCase();
    return n ? cmds.filter(c => c.label.toLowerCase().includes(n) || c.hint?.toLowerCase().includes(n)) : cmds;
  }, [q, cmds]);

  // A shortcut (R / X) opens the palette already on the confirm step.
  useEffect(() => {
    const id = getState().paletteConfirm;
    if (!id) return;
    setState({ paletteConfirm: null });
    const c = cmds.find(x => x.id === id);
    if (c) setConfirming(c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { inputRef.current?.focus(); }, [confirming]);
  useEffect(() => { setSel(0); }, [q]);

  const exec = async (c: Cmd) => {
    if (c.confirm && confirming?.id !== c.id) { setConfirming(c); return; }
    try {
      await c.run();
      if (c.confirm) { setNote(c.id === 'shutdown' ? 'Shutting down…' : 'Restarting…'); window.setTimeout(close, 1200); }
    } catch (e) {
      setNote(String((e as Error).message || e));
    }
  };

  return (
    <div className="fixed inset-0 z-[var(--z-palette)] flex items-start justify-center pt-[14vh]" role="dialog" aria-modal="true" aria-label="Command palette">
      <button className="drawer-scrim absolute inset-0" aria-label="Close" onClick={close} />
      <div className="palette relative w-full max-w-lg overflow-hidden">
        {confirming ? (
          <div className="space-y-3 p-5">
            <p className="text-body t1">{confirming.confirm}</p>
            {note ? <p className="text-small t2">{note}</p> : (
              <div className="flex gap-2">
                <button ref={el => el?.focus()} className="mini-btn mini-btn-danger" onClick={() => void exec(confirming)}>Confirm</button>
                <button className="mini-btn" onClick={() => setConfirming(null)}>Cancel</button>
              </div>
            )}
          </div>
        ) : (
          <>
            <div className="flex items-center gap-2 border-b border-[var(--line)] px-4 py-3">
              <Search className="h-4 w-4 t3" />
              <input
                ref={inputRef}
                value={q}
                onChange={e => setQ(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'ArrowDown') { e.preventDefault(); setSel(s => Math.min(shown.length - 1, s + 1)); }
                  else if (e.key === 'ArrowUp') { e.preventDefault(); setSel(s => Math.max(0, s - 1)); }
                  else if (e.key === 'Enter' && shown[sel]) { e.preventDefault(); void exec(shown[sel]); }
                }}
                placeholder="Type a command…"
                className="w-full bg-transparent text-body t1 outline-none placeholder:text-[var(--text-3)]"
              />
              <span className="micro">ESC</span>
            </div>
            <ul className="custom-scrollbar max-h-[50vh] overflow-y-auto p-1.5" role="listbox">
              {shown.length === 0 && <li className="px-3 py-2 text-small t3">No matching command.</li>}
              {shown.map((c, i) => (
                <li key={c.id} role="option" aria-selected={i === sel}>
                  <button
                    className={`palette-row ${i === sel ? 'palette-row-sel' : ''}`}
                    onMouseEnter={() => setSel(i)}
                    onClick={() => void exec(c)}
                  >
                    <c.Icon className="h-4 w-4 shrink-0 ac-text" />
                    <span className="text-body t1">{c.label}</span>
                    <span className="ml-auto flex min-w-0 items-center gap-2">
                      {c.hint && <span className="truncate text-meta t3">{c.hint}</span>}
                      {c.sc && <kbd className="kbd">{keyFor(c.sc)}</kbd>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

