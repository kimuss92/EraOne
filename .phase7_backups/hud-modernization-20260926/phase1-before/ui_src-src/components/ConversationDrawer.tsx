import { useEffect, useMemo, useRef, useState } from 'react';
import { ClipboardCheck, Copy, Search, X } from 'lucide-react';
import Drawer from './Drawer';
import { useHud } from '../lib/store';
import type { ConversationEntry } from '../lib/types';

// Full conversation history. Seeded from the backend's record (snapshot on
// connect), so a reloaded HUD keeps the thread — the old UI lost it on reload.

type Role = 'all' | 'user' | 'agent' | 'system';
const NAMES: Record<ConversationEntry['role'], string> = { user: 'YOU', agent: 'ERA ONE', system: 'SYSTEM' };

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

export default function ConversationDrawer() {
  const entries = useHud(s => s.conversation);
  const [q, setQ] = useState('');
  const [role, setRole] = useState<Role>('all');
  const [copied, setCopied] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const filtering = q !== '' || role !== 'all';

  const visible = useMemo(() => {
    const needle = q.toLowerCase();
    return entries.filter(e => (role === 'all' || e.role === role) && (!needle || e.text.toLowerCase().includes(needle)));
  }, [entries, q, role]);

  useEffect(() => {
    if (!filtering) endRef.current?.scrollIntoView({ block: 'end' });
  }, [visible.length, filtering]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === 'f' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const copy = async (id: string, text: string) => {
    if (await copyText(text)) {
      setCopied(id);
      window.setTimeout(() => setCopied(c => (c === id ? null : c)), 1500);
    }
  };

  return (
    <Drawer
      title="CONVERSATION"
      actions={
        <button
          className="icon-btn"
          title="Copy the visible conversation"
          onClick={() => copy('__all__', visible.map(e => `${NAMES[e.role]} [${new Date(e.ts * 1000).toLocaleTimeString()}]: ${e.text}`).join('\n'))}
        >
          {copied === '__all__' ? <ClipboardCheck className="h-4 w-4 text-[var(--ok)]" /> : <Copy className="h-4 w-4" />}
        </button>
      }
    >
      <div className="sticky top-0 z-10 space-y-2 border-b border-[var(--line)] bg-[var(--drawer)] p-3">
        <div className="field flex items-center gap-1.5 px-2 py-1.5">
          <Search className="h-3.5 w-3.5 t3" />
          <input
            ref={searchRef}
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="Filter conversation… (Ctrl+F)"
            className="w-full bg-transparent text-[12px] t1 outline-none placeholder:text-[var(--text-3)]"
          />
          {q && <button onClick={() => setQ('')} aria-label="Clear"><X className="h-3.5 w-3.5 t3" /></button>}
        </div>
        <div className="flex items-center gap-1">
          {(['all', 'user', 'agent', 'system'] as Role[]).map(r => (
            <button key={r} onClick={() => setRole(r)} className={`chip cursor-pointer ${role === r ? 'chip-live' : 'chip-dim'}`}>
              {r === 'all' ? 'ALL' : NAMES[r as ConversationEntry['role']]}
            </button>
          ))}
          <span className="ml-auto font-mono text-[10px] t3">{filtering ? `${visible.length}/${entries.length}` : entries.length}</span>
        </div>
      </div>
      <div className="space-y-3 p-4">
        {visible.length === 0 && <p className="text-[12px] t3">{entries.length ? 'Nothing matches the filter.' : 'No conversation yet.'}</p>}
        {visible.map(e => (
          <div key={e.id} className="group">
            <div className="flex items-center gap-2">
              <span className={`micro ${e.role === 'agent' ? 'ac-text' : e.role === 'user' ? 't1' : ''}`}>{NAMES[e.role]}</span>
              {e.source && e.source !== 'typed' && <span className="chip chip-dim">{e.source.toUpperCase()}</span>}
              <span className="font-mono text-[10px] t3">{new Date(e.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
              <button className="ml-auto opacity-0 transition group-hover:opacity-100" title="Copy" onClick={() => copy(e.id, e.text)}>
                {copied === e.id ? <ClipboardCheck className="h-3 w-3 text-[var(--ok)]" /> : <Copy className="h-3 w-3 t3" />}
              </button>
            </div>
            <p className={`mt-0.5 select-text whitespace-pre-wrap break-words text-[12.5px] leading-snug ${e.role === 'system' ? (e.level === 'error' ? 'text-[var(--bad)]' : e.level === 'warning' ? 'text-[var(--warn)]' : 't3') : 't1'}`}>
              {e.text}
            </p>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </Drawer>
  );
}
