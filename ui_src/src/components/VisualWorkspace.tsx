import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/transport';
import { setState, useHud } from '../lib/store';
import type { DrawerId } from '../lib/types';
type Widget = { id: string; kind: string; label: string; value?: string | number; series?: number[]; binding?: 'cpu_percent' | 'ram_percent' | 'development'; action?: { drawer: DrawerId } };
type Scene = { revision: number; bundle?: string; layout: { theme: 'light' | 'dark'; left: boolean; right: boolean; columns: number; accent: string; density: string }; widgets: Widget[] };
type Job = { id: string; request: string; state: string; progress: number; plan: string; error?: string; approval: string; reload: boolean; requires_restart?: boolean; files: { path: string; diff: string }[]; checks: Record<string, unknown> };
function Plot({ value, series, accent }: { value?: number; series?: number[]; accent: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const node = canvas.current, ctx = node?.getContext('2d'); if (!node || !ctx) return;
    let frame = 0, last = 0;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    const draw = (time: number) => {
      if (!document.hidden && time - last >= 33) {
        last = time; const width = node.clientWidth, height = 60, dpr = Math.min(window.devicePixelRatio || 1, 2);
        if (node.width !== Math.round(width * dpr) || node.height !== height * dpr) { node.width = Math.round(width * dpr); node.height = height * dpr; }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height); ctx.fillStyle = accent; ctx.strokeStyle = accent;
        if (series) {
          if (series.length) { const min = Math.min(...series), max = Math.max(...series), span = max - min || 1; ctx.beginPath(); series.forEach((point, i) => { const x = i * width / Math.max(1, series.length - 1), y = height - 5 - (point - min) / span * (height - 10); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke(); }
        } else {
          const level = height * (1 - Math.max(0, Math.min(100, value ?? 0)) / 100); ctx.beginPath(); ctx.moveTo(0, height);
          for (let x = 0; x <= width; x += 3) ctx.lineTo(x, level + (value && value < 100 ? Math.sin(x / 28 + (reduced.matches ? 0 : time / 1000)) * 3 : 0));
          ctx.lineTo(width, height); ctx.closePath(); ctx.globalAlpha = .6; ctx.fill(); ctx.globalAlpha = 1;
        }
      }
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw); return () => cancelAnimationFrame(frame);
  }, [value, series, accent]);
  return <canvas ref={canvas} style={{ width: '100%', height: 60 }} aria-hidden="true" />;
}
export default function VisualWorkspace() {
  const [scene, sceneSet] = useState<Scene | null>(null), [jobs, jobsSet] = useState<Job[]>([]), [open, openSet] = useState(false), [request, requestSet] = useState(''), [error, errorSet] = useState(''), [busy, busySet] = useState(false), [reviewed, reviewedSet] = useState<Record<string, string>>({});
  const metrics = useHud(s => s.metrics), bundle = useRef(''), active = useRef(true);
  useEffect(() => {
    active.current = true; let loading = false;
    const load = async () => {
      if (loading) return; loading = true;
      try {
        const [next, development] = await Promise.all([api<Scene>('/api/visual'), api<{ jobs: Job[] }>('/api/development')]); if (!active.current) return;
        sceneSet(next); jobsSet(development.jobs);
        if (bundle.current && next.bundle && next.bundle !== bundle.current && development.jobs.some(j => j.state === 'activated' && j.reload)) { window.location.reload(); return; }
        bundle.current = next.bundle || '';
      } catch { /* Mutations report errors; polling recovers after reconnect. */ }
      finally { loading = false; }
    };
    load(); const timer = window.setInterval(load, 3000); window.addEventListener('era:visual', load); window.addEventListener('era:development', load);
    return () => { active.current = false; clearInterval(timer); window.removeEventListener('era:visual', load); window.removeEventListener('era:development', load); };
  }, []);
  const revision = scene?.revision;
  useEffect(() => {
    if (!scene || scene.revision === 0) return;
    setState({ theme: scene.layout.theme, showLeft: scene.layout.left, showRight: scene.layout.right }); document.documentElement.style.setProperty('--visual-accent', scene.layout.accent);
  }, [revision]);
  const mutate = async (path: string, body: unknown) => {
    busySet(true); errorSet('');
    try { await api(path, { method: 'POST', body }); window.dispatchEvent(new Event('era:development')); window.dispatchEvent(new Event('era:visual')); }
    catch (e) { errorSet(e instanceof Error ? e.message : String(e)); } finally { busySet(false); }
  };
  const pending = jobs.filter(j => j.state === 'awaiting_approval').length;
  return <section className="shrink-0 rounded border border-[var(--line)] p-2 mb-2" aria-label="Live visual workspace">
    <div className="flex items-center gap-2"><span className="micro flex-1">LIVE WORKSPACE</span><button className="mini-btn" onClick={() => setState({ drawer: 'schedule' })}>Tasks</button><button className="mini-btn" onClick={() => openSet(!open)}>Development{pending ? ` (${pending} awaiting approval)` : ''}</button></div>
    {scene && scene.widgets.length > 0 && <div className="grid gap-2 mt-2 max-h-48 overflow-auto" style={{ gridTemplateColumns: `repeat(${scene.layout.columns},minmax(0,1fr))`, padding: scene.layout.density === 'compact' ? 0 : 4 }}>
      {scene.widgets.map(w => {
        const value = w.binding === 'development' ? (jobs[0]?.progress ?? 0) : w.binding ? metrics[w.binding] : w.value;
        return <article key={w.id} className="rounded border border-[var(--line)] p-2 min-w-0"><label className="text-meta t2" htmlFor={`visual-${w.id}`}>{w.label}</label>
          {w.kind === 'progress' || w.kind === 'chart' ? <><Plot value={typeof value === 'number' ? value : 0} series={w.kind === 'chart' ? w.series || [] : undefined} accent={scene.layout.accent} /><p className="text-meta t2">{w.kind === 'progress' ? `${value ?? '—'}%` : `${w.series?.length || 0} samples`}{w.binding === 'development' && jobs[0] ? ` · ${jobs[0].state.replace(/_/g, ' ')}` : ''}</p></> : w.kind === 'button' ? <button className="mini-btn block mt-1" onClick={() => { if (w.action) setState({ drawer: w.action.drawer }); }}>{String(value ?? w.label)}</button> : w.kind === 'field' ? <input key={`${w.id}-${revision}`} id={`visual-${w.id}`} className="w-full bg-transparent border border-[var(--line)] p-1" maxLength={300} defaultValue={String(value ?? '')} onBlur={e => { if (e.target.value !== String(value ?? '')) mutate('/api/visual', { action: 'upsert', revision, widget: { ...w, value: e.target.value } }); }} /> : <p className="text-body t1 break-words">{String(value ?? '—')}</p>}
        </article>;
      })}
    </div>}
    {open && <div className="mt-3 max-h-80 overflow-auto space-y-3">
      <form onSubmit={e => { e.preventDefault(); mutate('/api/development', { action: 'propose', request }); }} className="flex gap-2"><input aria-label="Feature request" className="min-w-0 flex-1 bg-transparent border border-[var(--line)] p-2 text-small" placeholder="Describe a feature to generate and verify…" maxLength={8000} required value={request} onChange={e => requestSet(e.target.value)} /><button className="mini-btn" disabled={busy}>Stage feature</button></form>
      <p className="text-meta t3">Claude generates a candidate. Tests and diagnosis run before your final approval. Engine Python edits require restart; visual scenes and approved feature modules reload live.</p>
      {jobs.map(j => <article className="border border-[var(--line)] rounded p-2 space-y-2" key={j.id}>
        <p className="text-small t1">{j.request}</p><p className="text-meta t2">{j.state.replace(/_/g, ' ')} · {j.progress}%{j.requires_restart ? ' · engine restart required' : ''}</p><progress className="w-full h-1" max={100} value={j.progress} aria-label="Verification progress" />
        {j.error && <p role="alert" className="text-meta text-[var(--bad)]">{j.error}</p>}{j.plan && <p className="text-meta t2">{j.plan}</p>}
        <details><summary className="text-meta t2 cursor-pointer">Changes and verification ({j.files.length} files)</summary><pre className="text-micro whitespace-pre-wrap break-words max-h-40 overflow-auto">{JSON.stringify(j.checks, null, 2)}</pre>{j.files.map(f => <details key={f.path}><summary className="text-meta t2">{f.path}</summary><pre className="text-micro whitespace-pre-wrap break-words max-h-48 overflow-auto">{f.diff}</pre></details>)}</details>
        {j.state === 'awaiting_approval' && <><label className="text-meta t2 flex gap-2"><input type="checkbox" checked={reviewed[j.id] === j.approval} onChange={e => reviewedSet({ ...reviewed, [j.id]: e.target.checked ? j.approval : '' })} />I reviewed the changes and verification results.</label><div className="flex gap-2"><button className="mini-btn" disabled={busy || reviewed[j.id] !== j.approval} onClick={() => mutate('/api/development', { action: 'approve', id: j.id, approval: j.approval, confirmed: true })}>Approve and activate</button><button className="mini-btn" disabled={busy} onClick={() => mutate('/api/development', { action: 'reject', id: j.id })}>Reject</button></div></>}
      </article>)}
    </div>}{error && <p role="alert" className="text-meta text-[var(--bad)] mt-2">{error}</p>}
  </section>;
}
