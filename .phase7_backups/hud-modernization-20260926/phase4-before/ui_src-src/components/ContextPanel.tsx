import { memo, useEffect, useState, type ReactNode } from 'react';
import { Brain, CloudSun, Cpu, Droplets, ExternalLink, FolderOpen, Globe, Search, Wind } from 'lucide-react';
import { CATEGORY_ICON } from './ActivityPanel';
import { useHud, useNow } from '../lib/store';
import { actions, api } from '../lib/transport';
import { ago, shortPath } from '../lib/format';
import type { ContextCard, Source } from '../lib/types';

// ── Context card renderer registry ─────────────────────────────────────
// Cards arrive from app/hud/tool_adapter.py with a `type`. Each type maps to a
// renderer; anything unknown falls back to `generic`, so a new backend card
// type shows up without a HUD change.

type Renderer = (c: ContextCard, open: boolean) => ReactNode;

// Scannable category names; the raw backend type stays the key.
const TYPE_LABEL: Record<string, string> = {
  generic: 'TOOL', file: 'FILE', web: 'WEB', memory: 'MEMORY', system: 'SYSTEM',
  email: 'EMAIL', task: 'TASK', weather: 'WEATHER', media: 'MEDIA', vision: 'VISION',
};

/** Cards below this index start collapsed to their title. */
const LEAD_CARDS = 3;

/** A relative time that stays current (the panel itself does not re-render). */
function Ago({ ts }: { ts: number | null | undefined }) {
  const now = useNow();
  return <>{ago(ts, now)}</>;
}

function payloadLine(c: ContextCard, key: string): string {
  const v = c.payload?.[key];
  return v === undefined || v === null || v === '' ? '' : String(v);
}

const RENDERERS: Record<string, Renderer> = {
  web: c => <p className="card-body">{c.subtitle}{payloadLine(c, 'results') && <span className="t3"> · {payloadLine(c, 'results')} sources</span>}</p>,
  memory: c => <p className="card-body">{payloadLine(c, 'value') || c.subtitle}</p>,
  weather: c => <p className="card-body">{c.subtitle}</p>,
  media: c => <p className="card-body">{c.subtitle}</p>,
  file: (c, open) => {
    const path = payloadLine(c, 'path');
    return (
      <>
        {path && (
          <p className="card-body break-all font-mono !text-micro" title={path}>{open ? path : shortPath(path)}</p>
        )}
        <p className="card-body">{c.subtitle}</p>
      </>
    );
  },
  email: c => <p className="card-body">{c.subtitle}</p>,
  system: c => <p className="card-body">{c.subtitle}</p>,
  task: c => <p className="card-body">{c.subtitle}</p>,
  generic: c => <p className="card-body">{c.subtitle}</p>,
};

function Card({ card, lead }: { card: ContextCard; lead: boolean }) {
  const Icon = CATEGORY_ICON[card.icon] || Cpu;
  const render = RENDERERS[card.type] || RENDERERS.generic;
  const [msg, setMsg] = useState('');
  const [open, setOpen] = useState(false);
  const toggle = () => { if (!window.getSelection()?.toString()) setOpen(o => !o); };
  return (
    <article className="ctx-card">
      <div className="flex items-center gap-2">
        <Icon className={`h-3.5 w-3.5 shrink-0 ${lead ? 'ac-text' : 't3'}`} />
        <span className="micro">{TYPE_LABEL[card.type] || card.type.toUpperCase()}</span>
        <span className="ml-auto font-mono text-micro t3"><Ago ts={card.timestamp} /></span>
      </div>
      <h4 className="mt-1 min-w-0">
        <button
          type="button"
          className={`block w-full truncate text-left font-medium ${lead ? 'text-body t1' : 'text-small t2'}`}
          aria-expanded={open}
          title={open ? 'Show less' : 'Show details'}
          onClick={() => setOpen(o => !o)}
        >
          {card.title}
        </button>
      </h4>
      {(lead || open) && (
        <div className={open ? 'select-text' : 'ctx-clamp cursor-pointer'} onClick={toggle}>
          {render(card, open)}
        </div>
      )}
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
          {msg && <span className="text-micro text-[var(--warn)]">{msg}</span>}
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
        <span className="block truncate text-small t1">{src.title}</span>
        {src.subtitle && <span className="block truncate text-meta t3">{src.subtitle}</span>}
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
        <div className="kv"><span className="micro">UPDATED</span><span><Ago ts={overview?.lastUpdate} /></span></div>
      </div>
      {cats.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {cats.map(([k, v]) => <span key={k} className="arg-chip"><span className="t3">{k}</span> {v}</span>)}
        </div>
      )}
      <form onSubmit={search} className="field flex items-center gap-1.5 px-2 py-1">
        <Search className="h-3 w-3 t3" />
        <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)} placeholder="Search memory…" aria-label="Search memory" className="w-full bg-transparent text-small t1 outline-none placeholder:text-[var(--text-3)]" />
      </form>
      {err && <p className="text-meta text-[var(--warn)]">{err}</p>}
      {results && (
        <div className="custom-scrollbar max-h-40 space-y-1 overflow-y-auto">
          {results.length === 0 && <p className="text-meta t3">No matching memories.</p>}
          {results.map(r => (
            <div key={`${r.category}/${r.key}`} className="src-row">
              <Brain className="mt-0.5 h-3.5 w-3.5 shrink-0 ac-text" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-small t1">{r.key}</span>
                <span className="block text-meta leading-snug t3">{r.category} · {r.value}</span>
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
  if (!w) return <p className="text-meta t3">Weather unavailable.</p>;
  if (!w.ok) return <p className="text-meta t3">{w.city}: weather unavailable ({w.error}).</p>;
  const r = (v: number | null | undefined) => (v === null || v === undefined ? 'N/A' : Math.round(v));
  return (
    <div className="flex items-center gap-3">
      <div className="font-mono text-display font-light leading-none tabular-nums t1">{r(w.temperature)}°</div>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="truncate text-small t1">{w.city}{w.country ? `, ${w.country}` : ''} · <span className="capitalize t2">{w.sky}</span></div>
        <div className="flex gap-3 font-mono text-meta t3">
          <span>H {r(w.high)}° L {r(w.low)}°</span>
          <span className="flex items-center gap-0.5"><Droplets className="h-3 w-3" />{r(w.humidity)}%</span>
          <span className="flex items-center gap-0.5"><Wind className="h-3 w-3" />{r(w.wind_kmh)} km/h</span>
        </div>
      </div>
    </div>
  );
}

function ContextPanel() {
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
  // Context + Sources scroll here, and only here. Memory and Weather are
  // separate fixed modules (below), so they never scroll out of view.
  return (
    <section className="panel flex min-h-[120px] flex-1 flex-col" aria-label="Context">
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto">
        <div className="panel-head sticky top-0 z-10"><span className="micro panel-title">CONTEXT</span><span className="ml-auto font-mono text-micro t3">{cards.length}</span></div>
        <div className="space-y-2 p-3">
          {recentCards.length === 0 && (
            <p className="text-meta leading-relaxed t3">No active context. Files, searches, memories and system results appear here as ERA ONE works.</p>
          )}
          {recentCards.map((c, i) => <Card key={c.id} card={c} lead={i < LEAD_CARDS} />)}
        </div>
        <div className="panel-head sticky top-0 z-10"><span className="micro panel-title">SOURCES</span><span className="ml-auto font-mono text-micro t3">{sources.length}</span></div>
        <div className="space-y-1 p-3">
          {recentSources.length === 0 && <p className="text-meta t3">No sources used yet.</p>}
          {recentSources.map(s => <SourceRow key={s.id} src={s} cards={cards} />)}
        </div>
      </div>
    </section>
  );
}

export const MemoryPanel = memo(function MemoryPanel({ searchRef }: { searchRef: React.RefObject<HTMLInputElement | null> }) {
  return (
    <section className="panel shrink-0" aria-label="Memory">
      <div className="panel-head"><span className="micro panel-title">MEMORY</span></div>
      <div className="p-3"><MemorySection searchRef={searchRef} /></div>
    </section>
  );
});

export const WeatherPanel = memo(function WeatherPanel() {
  return (
    <section className="panel shrink-0" aria-label="Weather">
      <div className="panel-head"><span className="micro panel-title">WEATHER</span></div>
      <div className="p-3"><WeatherSection /></div>
    </section>
  );
});

export default memo(ContextPanel);
