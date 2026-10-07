import { memo, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Brain, ChevronDown, Cloud, CloudDrizzle, CloudFog, CloudLightning, CloudMoon, CloudRain, CloudSnow,
  CloudSun, CloudSunRain, Cpu, Droplets, ExternalLink, FolderOpen, Globe, Moon, Search, Sun, Sunrise,
  Sunset, Umbrella, Wind, type LucideIcon,
} from 'lucide-react';
import { CATEGORY_ICON } from './ActivityPanel';
import { useHud, useNow } from '../lib/store';
import { actions, api } from '../lib/transport';
import { ago, clock, shortPath } from '../lib/format';
import { groupCards, stripDecor, tidy, toolOf, type CardGroup } from '../lib/context';
import PathText, { openPath } from './PathText';
import type { ContextCard, Source } from '../lib/types';

// ── Context card renderer registry ─────────────────────────────────────
// Cards arrive from app/hud/tool_adapter.py with a `type`. Each type maps to a
// renderer; anything unknown falls back to `generic`, so a new backend card
// type shows up without a HUD change. Collapsed, a card shows the TIDY text
// (lib/context.ts: no emoji, short paths); expanded, the RAW text — raw detail
// is always one click away, never lost.

type Renderer = (c: ContextCard, open: boolean) => ReactNode;

/** Cards below this index start collapsed to their title. */
const LEAD_CARDS = 3;
const MAX_GROUPS = 12;

// Paths in the text are buttons (PathText); collapsed, they show shortened.
const txt = (c: ContextCard, open: boolean) => (
  open ? <PathText text={c.subtitle} /> : <PathText text={c.subtitle.trim()} short format={stripDecor} />
);

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
  web: (c, open) => <p className="card-body">{txt(c, open)}{payloadLine(c, 'results') && <span className="t3"> · {payloadLine(c, 'results')} sources</span>}</p>,
  memory: (c, open) => <p className="card-body">{payloadLine(c, 'value') ? <PathText text={payloadLine(c, 'value')} /> : txt(c, open)}</p>,
  file: (c, open) => {
    const path = payloadLine(c, 'path');
    // The path is shown on its own line, so a summary that only repeats it is dropped.
    const sub = c.subtitle.trim();
    const body = sub && sub !== path ? txt(c, open) : null;
    return (
      <>
        {path && (
          <p className="card-body break-all font-mono !text-micro">
            <button
              type="button"
              className="path-link text-left"
              title={`Open ${path}`}
              onClick={e => { e.stopPropagation(); openPath(path); }}
              onKeyDown={e => e.stopPropagation()}
            >
              {open ? path : shortPath(path)}
            </button>
          </p>
        )}
        {body && <p className="card-body">{body}</p>}
      </>
    );
  },
  generic: (c, open) => <p className="card-body">{txt(c, open)}</p>,
};

function Card({ group, lead }: { group: CardGroup; lead: boolean }) {
  const card = group.latest;
  const Icon = CATEGORY_ICON[card.icon] || Cpu;
  const render = RENDERERS[card.type] || RENDERERS.generic;
  const [msg, setMsg] = useState('');
  const [open, setOpen] = useState(false);
  const selecting = () => Boolean(window.getSelection()?.toString());
  const toggle = () => { if (!selecting()) setOpen(o => !o); };
  const times = group.earlier.length + 1;
  const tool = toolOf(card);
  // A card with a file/folder behind it is ONE big button: a click anywhere
  // opens it. Details then live behind the small chevron instead.
  const openable = card.actions?.some(a => a.id === 'reveal') ?? false;
  const fail = (e: { message?: string }) => setMsg(String(e.message || e));
  const openIt = () => {
    if (selecting()) return;
    setMsg('');
    actions.openPath({ cardId: card.id }).catch(fail);
  };
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  return (
    <article
      className={`ctx-card ${openable ? 'ctx-card-open' : ''}`}
      {...(openable && {
        role: 'button',
        tabIndex: 0,
        title: `Open ${payloadLine(card, 'path') || card.title}`,
        onClick: openIt,
        onKeyDown: (e: React.KeyboardEvent) => {
          if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openIt(); }
        },
      })}
    >
      <div className="flex items-center gap-2">
        <Icon className={`h-3.5 w-3.5 shrink-0 ${lead ? 'ac-text' : 't3'}`} aria-hidden="true" />
        <span className="micro">{group.category}</span>
        {times > 1 && <span className="chip chip-dim" title={`Seen ${times} times`}>×{times}</span>}
        <span className="ml-auto font-mono text-micro t3"><Ago ts={card.timestamp} /></span>
        {openable && (
          <button
            type="button"
            className="icon-btn icon-btn-xs"
            aria-expanded={open}
            aria-label={open ? 'Show less' : 'Show details'}
            title={open ? 'Show less' : 'Show details'}
            onClick={e => { stop(e); setOpen(o => !o); }}
            onKeyDown={stop}
          >
            <ChevronDown className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        )}
      </div>
      <h4 className="mt-1 min-w-0">
        {openable ? (
          <span className={`block w-full truncate font-medium ${lead ? 'text-body t1' : 'text-small t2'}`}>{card.title}</span>
        ) : (
          <button
            type="button"
            className={`block w-full truncate text-left font-medium ${lead ? 'text-body t1' : 'text-small t2'}`}
            aria-expanded={open}
            title={open ? 'Show less' : 'Show details'}
            onClick={() => setOpen(o => !o)}
          >
            {card.title}
          </button>
        )}
      </h4>
      {(lead || open) && (
        <div className={open ? 'select-text' : `ctx-clamp ${openable ? '' : 'cursor-pointer'}`} onClick={openable ? undefined : toggle}>
          {render(card, open)}
        </div>
      )}
      {open && (
        <div className="mt-1.5 space-y-1">
          <p className="font-mono text-micro t3">
            {tool && <>via {tool} · </>}{clock(card.timestamp)}
          </p>
          {group.earlier.length > 0 && (
            <ul className="space-y-0.5 border-t border-[var(--line)] pt-1">
              {group.earlier.slice(0, 5).map(e => (
                <li key={e.id} className="flex gap-2 text-micro t3">
                  <span className="shrink-0 font-mono tabular-nums">{clock(e.timestamp)}</span>
                  <span className="min-w-0 truncate" title={e.subtitle}>{tidy(e.subtitle) || e.title}</span>
                </li>
              ))}
              {group.earlier.length > 5 && <li className="text-micro t3">+{group.earlier.length - 5} earlier</li>}
            </ul>
          )}
        </div>
      )}
      {openable && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <button type="button" className="source-action" onClick={e => { stop(e); openIt(); }} onKeyDown={stop}><ExternalLink className="h-3 w-3" aria-hidden="true" /> Open</button>
          <button
            type="button"
            className="source-action ml-auto"
            title="Show it selected in its folder"
            onClick={e => { stop(e); setMsg(''); actions.reveal(card.id).catch(fail); }}
            onKeyDown={stop}
          >
            <FolderOpen className="h-3 w-3" /> Show in folder
          </button>
        </div>
      )}
      {msg && <p className="mt-1 text-micro text-[var(--warn)]">{msg}</p>}
    </article>
  );
}

function SourceRow({ src, cards }: { src: Source; cards: ContextCard[] }) {
  const Icon = src.type === 'web' ? Globe : src.type === 'memory' ? Brain : src.type === 'file' ? FolderOpen : CloudSun;
  const fileCard = src.type === 'file' && src.toolCallId ? cards.find(c => c.id.startsWith(`${src.toolCallId}:`) && c.actions?.length) : undefined;
  // A file source whose subtitle is a full Windows path can be opened by the
  // backend straight from its source store, even once its card has scrolled out.
  const hasPath = src.type === 'file' && /^([a-z]:[\\/]|\\\\)/i.test(src.subtitle || '');
  const open = src.url?.startsWith('http')
    ? () => window.open(src.url, '_blank', 'noopener,noreferrer')
    : fileCard || hasPath
      ? () => void actions.openPath(fileCard ? { cardId: fileCard.id } : { sourceId: src.id }).catch(() => {})
      : undefined;
  const body = (
    <>
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 ac-text" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-small t1">{src.title}</span>
        {src.subtitle && <span className="block truncate text-meta t3">{tidy(src.subtitle)}</span>}
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
        <div className="mem-cats flex flex-wrap gap-1">
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
  feels_like?: number | null;
  humidity?: number | null;
  wind_kmh?: number | null;
  gusts_kmh?: number | null;
  wind_dir?: string;
  icon?: string;
  sky?: string;
  high?: number | null;
  low?: number | null;
  rain_chance?: number | null;
  rain_ahead?: { time: string; chance: number } | null;
  next_sun?: { kind: 'sunrise' | 'sunset'; time: string } | null;
  observed_at?: string | null;
  error?: string;
}

// Icon key from actions/weather_report.icon_key → glyph + tint. Unknown keys
// (an older backend sends none) fall back to a plain cloud.
const WEATHER_ICON: Record<string, [LucideIcon, string]> = {
  'clear-day': [Sun, 'text-[#ffc94d]'],
  'clear-night': [Moon, 'text-[var(--text-2)]'],
  'partly-day': [CloudSun, 'text-[#ffd98a]'],
  'partly-night': [CloudMoon, 'text-[var(--text-2)]'],
  cloud: [Cloud, 'text-[var(--text-2)]'],
  fog: [CloudFog, 'text-[var(--text-2)]'],
  drizzle: [CloudDrizzle, 'text-[var(--era-blue)]'],
  rain: [CloudRain, 'text-[var(--era-blue)]'],
  showers: [CloudSunRain, 'text-[var(--era-blue)]'],
  snow: [CloudSnow, 'text-[var(--you)]'],
  storm: [CloudLightning, 'text-[var(--warn)]'],
};

const WEATHER_REFRESH_MS = 300_000; // backend caches 5 min; the nowcast moves every 15
const WEATHER_RETRY_MS = 60_000;

function WeatherSection() {
  const [w, setW] = useState<Weather | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let t = 0;
    const load = () => {
      window.clearTimeout(t);
      api<Weather>('/api/weather')
        .then(d => { setW(d); setFailed(false); t = window.setTimeout(load, d.ok ? WEATHER_REFRESH_MS : WEATHER_RETRY_MS); })
        // Keep the last good reading on screen; retry soon instead of in 10 min.
        .catch(() => { setFailed(true); t = window.setTimeout(load, WEATHER_RETRY_MS); });
    };
    const onVisible = () => { if (document.visibilityState === 'visible') load(); };
    load();
    window.addEventListener('era:settings', load);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener('era:settings', load);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
  if (!w) return <p className="text-meta t3">{failed ? 'Weather unavailable — retrying…' : 'Loading weather…'}</p>;
  if (!w.ok) return <p className="text-meta t3">{w.city}: weather unavailable ({w.error}) — retrying…</p>;
  const r = (v: number | null | undefined) => (v === null || v === undefined ? 'N/A' : Math.round(v));
  const [Glyph, tint] = WEATHER_ICON[w.icon || ''] || WEATHER_ICON.cloud;
  const feels = w.feels_like != null && w.temperature != null && Math.round(w.feels_like) !== Math.round(w.temperature);
  const gust = w.gusts_kmh != null && w.wind_kmh != null && w.gusts_kmh >= w.wind_kmh + 10;
  const updated = w.observed_at ? w.observed_at.slice(11, 16) : '';
  return (
    <div className="space-y-2" title={`${w.sky} · Open-Meteo, local time ${updated}`}>
      <div className="flex items-center gap-3">
        <Glyph className={`h-10 w-10 shrink-0 ${tint}`} strokeWidth={1.4} aria-label={w.sky} />
        <div className="shrink-0">
          <div className="font-mono text-display font-light leading-none tabular-nums t1">{r(w.temperature)}°</div>
          {feels && <div className="mt-0.5 font-mono text-micro t3">feels {r(w.feels_like)}°</div>}
        </div>
        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="truncate text-small t1">{w.city}{w.country ? `, ${w.country}` : ''}</div>
          <div className="truncate text-meta capitalize t2">{w.sky}</div>
          <div className="font-mono text-micro t3">H {r(w.high)}° · L {r(w.low)}°</div>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-meta t3">
        <span className="flex items-center gap-1" title="Chance of rain today"><Umbrella className="h-3 w-3" />{r(w.rain_chance)}%</span>
        <span className="flex items-center gap-1" title="Humidity"><Droplets className="h-3 w-3" />{r(w.humidity)}%</span>
        <span className="flex items-center gap-1" title="Wind">
          <Wind className="h-3 w-3" />{w.wind_dir ? `${w.wind_dir} ` : ''}{r(w.wind_kmh)}{gust ? `–${r(w.gusts_kmh)}` : ''} km/h
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-micro t3">
        {w.rain_ahead ? (
          <span className="flex items-center gap-1 text-[var(--era-blue)]"><CloudRain className="h-3 w-3" />Rain likely ~{w.rain_ahead.time} ({w.rain_ahead.chance}%)</span>
        ) : w.next_sun ? (
          <span className="flex items-center gap-1" title={`Next ${w.next_sun.kind}`}>
            {w.next_sun.kind === 'sunrise' ? <Sunrise className="h-3 w-3" /> : <Sunset className="h-3 w-3" />}{w.next_sun.time}
          </span>
        ) : null}
        {updated && <span className="ml-auto">{failed ? 'stale · ' : ''}updated {updated}</span>}
      </div>
    </div>
  );
}

function ContextPanel() {
  const cards = useHud(s => s.cards);
  const [filter, setFilter] = useState('ALL');
  // Newest first, repeated results folded into one card (lib/context.ts).
  const groups = useMemo(() => groupCards([...cards].reverse()), [cards]);
  const cats = useMemo(() => {
    const n = new Map<string, number>();
    groups.forEach(g => n.set(g.category, (n.get(g.category) || 0) + 1));
    return [...n.entries()];
  }, [groups]);
  // A filter whose category has scrolled out of the buffer falls back to ALL.
  const active = cats.some(([c]) => c === filter) ? filter : 'ALL';
  const shown = (active === 'ALL' ? groups : groups.filter(g => g.category === active)).slice(0, MAX_GROUPS);
  // Context scrolls here, and only here. Sources, Memory and Weather are
  // separate modules below it, so they never scroll out of view.
  return (
    <section className="panel flex min-h-[120px] flex-1 flex-col" aria-label="Context">
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto">
        <div className="sticky top-0 z-10">
          <div className="panel-head"><span className="micro panel-title">CONTEXT</span><span className="ml-auto font-mono text-micro t3">{groups.length}</span></div>
          {cats.length > 1 && (
            <div className="ctx-filters" role="group" aria-label="Filter context by category">
              {[['ALL', groups.length] as const, ...cats].map(([c, n]) => (
                <button
                  key={c}
                  type="button"
                  className={`chip ${active === c ? 'chip-live' : 'chip-dim'}`}
                  aria-pressed={active === c}
                  onClick={() => setFilter(c)}
                >
                  {c} {n}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="space-y-2 p-3">
          {groups.length === 0 && (
            <p className="text-meta leading-relaxed t3">No active context. Files, searches, memories and system results appear here as ERA ONE works.</p>
          )}
          {shown.map((g, i) => <Card key={g.latest.id} group={g} lead={i < LEAD_CARDS} />)}
        </div>
      </div>
    </section>
  );
}

// SOURCES — its own module (not a section at the bottom of the Context scroll),
// so what ERA ONE drew on is always in view. Bounded height, own scroll.
export const SourcesPanel = memo(function SourcesPanel() {
  const cards = useHud(s => s.cards);
  const sources = useHud(s => s.sources);
  // Newest first; the same source cited again (same URL, or same title for
  // URL-less ones) is listed once, at its latest use.
  const recent = useMemo(() => {
    const seen = new Set<string>();
    return [...sources].reverse().filter(s => {
      const key = s.url || `${s.type}|${s.title}|${s.subtitle || ''}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 12);
  }, [sources]);
  return (
    <section className="panel sources-panel flex shrink-0 flex-col" aria-label="Sources">
      <div className="panel-head"><span className="micro panel-title">SOURCES</span><span className="ml-auto font-mono text-micro t3">{recent.length}</span></div>
      <div className="custom-scrollbar min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
        {recent.length === 0 && <p className="px-1 text-meta t3">No sources used yet.</p>}
        {recent.map(s => <SourceRow key={s.id} src={s} cards={cards} />)}
      </div>
    </section>
  );
});

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
