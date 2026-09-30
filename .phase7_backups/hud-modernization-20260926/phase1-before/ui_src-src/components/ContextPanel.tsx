import { memo, useEffect, useState, type ReactNode } from 'react';
import { Brain, CloudSun, Cpu, Droplets, ExternalLink, FolderOpen, Globe, Search, Wind } from 'lucide-react';
import { CATEGORY_ICON } from './ActivityPanel';
import { useHud } from '../lib/store';
import { actions, api } from '../lib/transport';
import { ago } from '../lib/format';
import type { ContextCard, Source } from '../lib/types';

// ── Context card renderer registry ─────────────────────────────────────
// Cards arrive from app/hud/tool_adapter.py with a `type`. Each type maps to a
// renderer; anything unknown falls back to `generic`, so a new backend card
// type shows up without a HUD change.

type Renderer = (c: ContextCard) => ReactNode;

function payloadLine(c: ContextCard, key: string): string {
  const v = c.payload?.[key];
  return v === undefined || v === null || v === '' ? '' : String(v);
}

const RENDERERS: Record<string, Renderer> = {
  web: c => <p className="card-body">{c.subtitle}{payloadLine(c, 'results') && <span className="t3"> · {payloadLine(c, 'results')} sources</span>}</p>,
  memory: c => <p className="card-body">{payloadLine(c, 'value') || c.subtitle}</p>,
  weather: c => <p className="card-body">{c.subtitle}</p>,
  media: c => <p className="card-body">{c.subtitle}</p>,
  file: c => (
    <>
      <p className="card-body break-all font-mono !text-[10px]">{payloadLine(c, 'path')}</p>
      <p className="card-body">{c.subtitle}</p>
    </>
  ),
  email: c => <p className="card-body">{c.subtitle}</p>,
  system: c => <p className="card-body">{c.subtitle}</p>,
  task: c => <p className="card-body">{c.subtitle}</p>,
  generic: c => <p className="card-body">{c.subtitle}</p>,
};

function Card({ card }: { card: ContextCard }) {
  const Icon = CATEGORY_ICON[card.icon] || Cpu;
  const render = RENDERERS[card.type] || RENDERERS.generic;
  const [msg, setMsg] = useState('');
  return (
    <article className="ctx-card">
      <div className="flex items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0 ac-text" />
        <span className="micro !tracking-[0.18em]">{card.type}</span>
        <span className="ml-auto font-mono text-[10px] t3">{ago(card.timestamp)}</span>
      </div>
      <h4 className="mt-1 truncate text-[12.5px] font-medium t1">{card.title}</h4>
      {render(card)}
      {card.actions?.length > 0 && (
        <div className="mt-1.5 flex gap-1.5">
          {card.actions.map(a => (
            <button
              key={a.id}
              className="mini-btn"
              onClick={() => actions.reveal(card.id).then(() => setMsg('')).catch(e => setMsg(String(e.message || e)))}
            >
              <FolderOpen className="h-3 w-3" /> {a.label}
            </button>
          ))}
          {msg && <span className="text-[10px] text-[var(--warn)]">{msg}</span>}
        </div>
      )}
    </article>
  );
}

function SourceRow({ src, cards }: { src: Source; cards: ContextCard[] }) {
  const Icon = src.type === 'web' ? Globe : src.type === 'memory' ? Brain : src.type === 'file' ? FolderOpen : CloudSun;
  const fileCard = src.type === 'file' && src.toolCallId ? cards.find(c => c.id.startsWith(`${src.toolCallId}:`) && c.actions?.length) : undefined;
  const open = src.url?.startsWith('http')
    ? () => window.open(src.url, '_blank', 'noopener,noreferrer')
    : fileCard
      ? () => void actions.reveal(fileCard.id).catch(() => {})
      : undefined;
  const body = (
    <>
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 ac-text" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12px] t1">{src.title}</span>
        {src.subtitle && <span className="block truncate text-[10.5px] t3">{src.subtitle}</span>}
      </span>
      {open && <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 t3" />}
    </>
  );
  return open ? (
    <button type="button" onClick={open} className="src-row w-full text-left" title={src.url || src.subtitle}>{body}</button>
  ) : (
    <div className="src-row">{body}</div>
  );
}

interface MemoryOverview {
  entries: number | null;
  categories: Record<string, number>;
  lastUpdate: number | null;
}

function MemorySection({ searchRef }: { searchRef: React.RefObject<HTMLInputElement | null> }) {
  const session = useHud(s => s.memory);
  const conn = useHud(s => s.connection);
  const [overview, setOverview] = useState<MemoryOverview | null>(null);
  const [q, setQ] = useState('');
  const [results, setResults] = useState<{ category: string; key: string; value: string }[] | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    const load = () => api<MemoryOverview>('/api/memory').then(setOverview).catch(() => setOverview(null));
    load();
    const t = window.setInterval(load, 30_000);
    return () => window.clearInterval(t);
  }, [session.retrieved_total]);

  const search = (e: React.FormEvent) => {
    e.preventDefault();
    if (!q.trim()) { setResults(null); return; }
    api<{ results: { category: string; key: string; value: string }[] }>(`/api/memory/search?q=${encodeURIComponent(q)}`)
      .then(r => { setResults(r.results); setErr(''); })
      .catch(x => setErr(String(x.message || x)));
  };

  const cats = overview ? Object.entries(overview.categories).sort((a, b) => b[1] - a[1]).slice(0, 4) : [];
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        <div className="kv"><span className="micro">SESSION</span><span className={conn.kind === 'connected' || conn.kind === 'local' ? 'text-[var(--ok)]' : 'text-[var(--warn)]'}>{conn.kind === 'connected' || conn.kind === 'local' ? 'ACTIVE' : conn.label.toUpperCase()}</span></div>
        <div className="kv"><span className="micro">RETRIEVED</span><span>{session.retrieved_total}</span></div>
        <div className="kv"><span className="micro">ENTRIES</span><span>{overview?.entries ?? 'N/A'}</span></div>
        <div className="kv"><span className="micro">UPDATED</span><span>{ago(overview?.lastUpdate)}</span></div>
      </div>
      {cats.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {cats.map(([k, v]) => <span key={k} className="arg-chip"><span className="t3">{k}</span> {v}</span>)}
        </div>
      )}
      <form onSubmit={search} className="field flex items-center gap-1.5 px-2 py-1">
        <Search className="h-3 w-3 t3" />
        <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)} placeholder="Search memory…" className="w-full bg-transparent text-[11.5px] t1 outline-none placeholder:text-[var(--text-3)]" />
      </form>
      {err && <p className="text-[10.5px] text-[var(--warn)]">{err}</p>}
      {results && (
        <div className="space-y-1">
          {results.length === 0 && <p className="text-[11px] t3">No matching memories.</p>}
          {results.map(r => (
            <div key={`${r.category}/${r.key}`} className="src-row">
              <Brain className="mt-0.5 h-3.5 w-3.5 shrink-0 ac-text" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] t1">{r.key}</span>
                <span className="block text-[10.5px] leading-snug t3">{r.category} · {r.value}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface Weather {
  ok: boolean;
  city: string;
  country?: string;
  temperature?: number | null;
  humidity?: number | null;
  wind_kmh?: number | null;
  sky?: string;
  high?: number | null;
  low?: number | null;
  error?: string;
}

function WeatherSection() {
  const [w, setW] = useState<Weather | null>(null);
  useEffect(() => {
    const load = () => api<Weather>('/api/weather').then(setW).catch(() => setW(null));
    load();
    const t = window.setInterval(load, 600_000);
    window.addEventListener('era:settings', load);
    return () => { window.clearInterval(t); window.removeEventListener('era:settings', load); };
  }, []);
  if (!w) return <p className="text-[11px] t3">Weather unavailable.</p>;
  if (!w.ok) return <p className="text-[11px] t3">{w.city}: weather unavailable ({w.error}).</p>;
  const r = (v: number | null | undefined) => (v === null || v === undefined ? 'N/A' : Math.round(v));
  return (
    <div className="flex items-center gap-3">
      <div className="font-mono text-[26px] font-light leading-none tabular-nums t1">{r(w.temperature)}°</div>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="truncate text-[12px] t1">{w.city}{w.country ? `, ${w.country}` : ''} · <span className="capitalize t2">{w.sky}</span></div>
        <div className="flex gap-3 font-mono text-[10.5px] t3">
          <span>H {r(w.high)}° L {r(w.low)}°</span>
          <span className="flex items-center gap-0.5"><Droplets className="h-2.5 w-2.5" />{r(w.humidity)}%</span>
          <span className="flex items-center gap-0.5"><Wind className="h-2.5 w-2.5" />{r(w.wind_kmh)} km/h</span>
        </div>
      </div>
    </div>
  );
}

function ContextPanel({ memorySearchRef }: { memorySearchRef: React.RefObject<HTMLInputElement | null> }) {
  const cards = useHud(s => s.cards);
  const sources = useHud(s => s.sources);
  const recentCards = [...cards].reverse().slice(0, 10);
  // Newest first; the same source cited again (same URL, or same title for
  // URL-less ones) is listed once, at its latest use.
  const seen = new Set<string>();
  const recentSources = [...sources].reverse().filter(s => {
    const key = s.url || `${s.type}|${s.title}|${s.subtitle || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 12);
  return (
    <section className="panel flex min-h-0 flex-1 flex-col" aria-label="Context">
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto">
        <div className="panel-head sticky top-0 z-10"><span className="micro t2">CONTEXT</span><span className="ml-auto font-mono text-[10px] t3">{cards.length}</span></div>
        <div className="space-y-2 p-3">
          {recentCards.length === 0 && <p className="text-[11px] leading-relaxed t3">Results ERA ONE retrieves — files, searches, memories, media, system actions — appear here.</p>}
          {recentCards.map(c => <Card key={c.id} card={c} />)}
        </div>
        <div className="panel-head"><span className="micro t2">SOURCES</span><span className="ml-auto font-mono text-[10px] t3">{sources.length}</span></div>
        <div className="space-y-1 p-3">
          {recentSources.length === 0 && <p className="text-[11px] t3">No sources used yet.</p>}
          {recentSources.map(s => <SourceRow key={s.id} src={s} cards={cards} />)}
        </div>
        <div className="panel-head"><span className="micro t2">MEMORY</span></div>
        <div className="p-3"><MemorySection searchRef={memorySearchRef} /></div>
        <div className="panel-head"><span className="micro t2">WEATHER</span></div>
        <div className="p-3"><WeatherSection /></div>
      </div>
    </section>
  );
}

export default memo(ContextPanel);
