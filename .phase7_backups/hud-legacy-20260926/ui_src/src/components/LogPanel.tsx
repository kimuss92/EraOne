import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Terminal, Search, X, Check, Sparkles, Copy, ClipboardCheck } from 'lucide-react';
import type { LogEntry } from '../hooks/useEra';

// Clipboard write that survives the file:// origin: navigator.clipboard first
// (enabled via JavascriptCanAccessClipboard in ui.py), execCommand fallback.
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

interface Props {
  logs: LogEntry[];
  lastDroppedFiles: string[];
}

type RoleFilter = 'all' | 'user' | 'agent' | 'system';

const FILTERS: { id: RoleFilter; label: string }[] = [
  { id: 'all', label: 'ALL' },
  { id: 'user', label: 'YOU' },
  { id: 'agent', label: 'ERA' },
  { id: 'system', label: 'SYS' },
];

// Role accents are fixed (user=cyan, era=gold, system=neutral) so the
// conversation stays legible while the emotion accent recolors the chrome.
//
// #faa638 is hsl(34, 95%, 60%) — literally the colour the sphere paints its
// computation sparks with (SPARK_HUE in NeuralCore.tsx). Era's own voice in the
// log is the same gold as the dots firing in her core; keep the two in step if
// either ever changes.
//
// Apple-chat presentation (reference image): each message is a soft rounded
// bubble with a small coloured icon disc on the left — check for you, sparkle
// for Era, terminal for system — instead of the old border-left color strip.
const ROLE_META: Record<LogEntry['role'], { name: string; bubble: string; disc: string; Icon: typeof Check }> = {
  user:   { name: 'You', bubble: 'bg-cyan-400/[0.08]',    disc: 'bg-cyan-400/20 text-cyan-300',       Icon: Check },
  agent:  { name: 'Era', bubble: 'bg-[#faa638]/[0.08]',   disc: 'bg-[#faa638]/20 text-[#fbb45c]',     Icon: Sparkles },
  system: { name: 'Sys', bubble: 'bg-[var(--line-soft)]', disc: 'bg-[var(--line)] text-[var(--text-2)]', Icon: Terminal },
};

// Conversation log — persistent scrollback (cap in useEra/MAX_LOG) with fast
// retrieval on top: a text filter (Ctrl+F) and one-tap role filters. Filtering
// is a cheap in-memory pass over ≤500 short strings; no virtualization needed.
function LogPanel({ logs, lastDroppedFiles }: Props) {
  const logsEndRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [role, setRole] = useState<RoleFilter>('all');
  const [copiedId, setCopiedId] = useState<string | number | null>(null);

  const handleCopy = useCallback(async (id: string | number, text: string) => {
    if (await copyText(text)) {
      setCopiedId(id);
      setTimeout(() => setCopiedId(c => (c === id ? null : c)), 1500);
    }
  }, []);

  // While the pointer is over the conversation panel, tell Python (title
  // bridge, ui.py) to stop treating mouse drags as window-move — that is what
  // lets a drag SELECT text so it can be copied.
  const setNoDrag = useCallback((on: boolean) => {
    document.title = on ? 'cmd:nodrag:1' : 'cmd:nodrag:0';
  }, []);

  const filtering = query !== '' || role !== 'all';

  const visible = useMemo(() => {
    if (!filtering) return logs;
    const q = query.toLowerCase();
    return logs.filter(l =>
      (role === 'all' || l.role === role) &&
      (q === '' || l.text.toLowerCase().includes(q))
    );
  }, [logs, query, role, filtering]);

  // Autoscroll only while NOT filtering — a filtered view is a reading view.
  useEffect(() => {
    if (!filtering) logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs, filtering]);

  // Ctrl+F jumps to the log search (Ctrl+K / "/" own the command input)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === 'f' && e.ctrlKey) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    // A flow column in App's <main> row (was: an absolute overlay pinned to the
    // window, which slid on top of the core once the window got narrow).
    // Width mirrors the System Status rail (App.tsx aside) — the user wants the
    // two side panels identical in size, framing the sphere symmetrically.
    <section
      className="pointer-events-auto z-20 flex w-60 shrink-0 flex-col gap-2.5 rounded-3xl glass p-4 xl:w-72"
      onPointerEnter={() => setNoDrag(true)}
      onPointerLeave={() => setNoDrag(false)}
    >
      <div className="ac-text flex items-center gap-2 border-b border-line-soft pb-2.5 text-[12px] font-semibold tracking-wide">
        Conversation
        <button
          onClick={() =>
            handleCopy(
              '__all__',
              visible
                .map(l => `${ROLE_META[l.role].name} [${new Date(l.time).toLocaleTimeString()}]: ${l.text}`)
                .join('\n')
            )
          }
          className="t3 ml-auto cursor-pointer transition hover:opacity-70"
          title="Copy whole conversation"
          aria-label="Copy whole conversation"
        >
          {copiedId === '__all__' ? (
            <ClipboardCheck className="h-3.5 w-3.5 text-emerald-400" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
        </button>
        <span className="t3 font-mono text-[9px]">
          {filtering ? `${visible.length}/${logs.length}` : logs.length}
        </span>
      </div>

      {/* Retrieval row: search + role chips */}
      <div className="flex items-center gap-1.5">
        <div className="bg-field flex min-w-0 flex-1 items-center gap-1.5 rounded-lg px-2 py-1">
          <Search className="t3 h-3 w-3 shrink-0" />
          <input
            ref={searchRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Escape') { setQuery(''); (e.target as HTMLInputElement).blur(); } }}
            placeholder="Filter… (Ctrl+F)"
            className="t1 w-full min-w-0 bg-transparent text-[11px] placeholder:[color:var(--text-3)] focus:outline-none"
          />
          {query && (
            <button onClick={() => setQuery('')} className="t3 cursor-pointer hover:opacity-70" aria-label="Clear filter">
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
      <div className="flex gap-1">
        {FILTERS.map(f => (
          <button
            key={f.id}
            onClick={() => setRole(f.id)}
            className={`cursor-pointer rounded px-1.5 py-0.5 text-[9px] font-bold tracking-wider transition ${
              role === f.id ? 'ac-bg-soft ac-text' : 't3 hover:bg-[var(--line-soft)]'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="custom-scrollbar flex flex-1 flex-col gap-2 overflow-y-auto pr-1">
        {visible.length === 0 && (
          <div className="t3 mt-6 text-center text-[11px]">
            {logs.length === 0 ? 'No messages yet.' : 'Nothing matches the filter.'}
          </div>
        )}
        {visible.map(log => {
          const meta = ROLE_META[log.role];
          return (
            <div
              key={log.id}
              className={`group animate-fade-in-up flex items-start gap-2.5 rounded-2xl px-3 py-2.5 text-xs leading-snug ${meta.bubble}`}
            >
              <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${meta.disc}`}>
                <meta.Icon className="h-3 w-3" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="t3 mb-0.5 flex items-baseline justify-between gap-1 text-[10px] font-medium">
                  <span>{meta.name}</span>
                  <span className="flex items-center gap-1.5">
                    <button
                      onClick={() => handleCopy(log.id, log.text)}
                      className="cursor-pointer opacity-0 transition group-hover:opacity-100"
                      title="Copy message"
                      aria-label="Copy message"
                    >
                      {copiedId === log.id ? (
                        <ClipboardCheck className="h-3 w-3 text-emerald-400" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </button>
                    <span className="tabular-nums">
                      {new Date(log.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </span>
                  </span>
                </span>
                <span className="t1 block cursor-text select-text whitespace-pre-wrap break-words">{log.text}</span>
              </span>
            </div>
          );
        })}
        <div ref={logsEndRef} />
      </div>
      {lastDroppedFiles.length > 0 && (
        <div className="t3 truncate text-[10px]">
          Last: {lastDroppedFiles.join(', ')}
        </div>
      )}
    </section>
  );
}

// Memoized: re-renders only when a log entry or a dropped file changes, not
// on the ~12Hz voice-meter ticks that re-render App.
export default memo(LogPanel);
