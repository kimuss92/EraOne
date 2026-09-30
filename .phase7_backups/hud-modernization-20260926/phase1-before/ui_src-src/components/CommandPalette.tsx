import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Brain,
  Gauge,
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
import { setState, useHud } from '../lib/store';
import { actions } from '../lib/transport';

interface Cmd {
  id: string;
  label: string;
  hint?: string;
  Icon: typeof Mic;
  run: () => void | Promise<unknown>;
  confirm?: string;
}

interface Props {
  focusInput: () => void;
  focusMemorySearch: () => void;
}

export default function CommandPalette({ focusInput, focusMemorySearch }: Props) {
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
    { id: 'ask', label: 'Ask ERA ONE…', hint: 'Focus the command bar', Icon: Terminal, run: () => { close(); focusInput(); } },
    micActive
      ? { id: 'mic', label: 'Mute microphone', Icon: MicOff, run: () => { actions.setMic(false); close(); } }
      : { id: 'mic', label: 'Start listening', hint: 'Turn the microphone on', Icon: Mic, run: () => { actions.setMic(true); close(); } },
    { id: 'stop', label: 'Stop speaking', hint: 'Esc', Icon: Square, run: () => { actions.interrupt(); close(); } },
    standby
      ? { id: 'standby', label: 'Wake ERA ONE', Icon: Power, run: () => { actions.setStandby(false); close(); } }
      : { id: 'standby', label: 'Enter standby', hint: 'Sleep until the wake word', Icon: Power, run: () => { actions.setStandby(true); close(); } },
    { id: 'conv', label: 'Open conversation', Icon: MessageSquareText, run: () => setState({ palette: false, drawer: 'conversation' }) },
    { id: 'memory', label: 'Search memory…', Icon: Brain, run: () => { setState({ palette: false, showRight: true }); window.setTimeout(focusMemorySearch, 50); } },
    { id: 'caps', label: 'Open capabilities', Icon: Layers, run: () => setState({ palette: false, drawer: 'capabilities' }) },
    { id: 'activity', label: 'Toggle live activity panel', Icon: PanelLeft, run: () => setState(s => ({ palette: false, showLeft: !s.showLeft })) },
    { id: 'context', label: 'Toggle context panel', Icon: PanelRight, run: () => setState(s => ({ palette: false, showRight: !s.showRight })) },
    { id: 'diag', label: 'Open diagnostics', Icon: Gauge, run: () => setState({ palette: false, drawer: 'diagnostics' }) },
    { id: 'settings', label: 'Settings', Icon: Settings, run: () => setState({ palette: false, drawer: 'settings' }) },
    { id: 'fs', label: 'Toggle fullscreen', Icon: Maximize, run: () => { close(); if (document.fullscreenElement) void document.exitFullscreen(); else void document.documentElement.requestFullscreen?.().catch(() => {}); } },
    { id: 'theme', label: theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme', Icon: theme === 'dark' ? Sun : Moon, run: () => setState(s => ({ palette: false, theme: s.theme === 'dark' ? 'light' : 'dark' })) },
    { id: 'restart', label: 'Restart ERA ONE', hint: 'The HUD reconnects by itself', Icon: RotateCcw, confirm: 'Restart ERA ONE now?', run: () => actions.restart() },
    { id: 'shutdown', label: 'Shutdown ERA ONE', hint: 'Stops the backend; double-click ERA ONE to start again', Icon: Activity, confirm: 'Shut ERA ONE down completely?', run: () => actions.shutdown() },
  ], [micActive, standby, theme, focusInput, focusMemorySearch]);

  const shown = useMemo(() => {
    const n = q.trim().toLowerCase();
    return n ? cmds.filter(c => c.label.toLowerCase().includes(n) || c.hint?.toLowerCase().includes(n)) : cmds;
  }, [q, cmds]);

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
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[14vh]" role="dialog" aria-modal="true" aria-label="Command palette">
      <button className="drawer-scrim absolute inset-0" aria-label="Close" onClick={close} />
      <div className="palette relative w-full max-w-lg overflow-hidden">
        {confirming ? (
          <div className="space-y-3 p-5">
            <p className="text-[14px] t1">{confirming.confirm}</p>
            {note ? <p className="text-[12px] t2">{note}</p> : (
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
                className="w-full bg-transparent text-[14px] t1 outline-none placeholder:text-[var(--text-3)]"
              />
              <span className="micro">ESC</span>
            </div>
            <ul className="custom-scrollbar max-h-[50vh] overflow-y-auto p-1.5" role="listbox">
              {shown.length === 0 && <li className="px-3 py-2 text-[12px] t3">No matching command.</li>}
              {shown.map((c, i) => (
                <li key={c.id} role="option" aria-selected={i === sel}>
                  <button
                    className={`palette-row ${i === sel ? 'palette-row-sel' : ''}`}
                    onMouseEnter={() => setSel(i)}
                    onClick={() => void exec(c)}
                  >
                    <c.Icon className="h-4 w-4 shrink-0 ac-text" />
                    <span className="text-[13px] t1">{c.label}</span>
                    {c.hint && <span className="ml-auto truncate text-[11px] t3">{c.hint}</span>}
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

