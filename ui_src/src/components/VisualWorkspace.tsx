import { useEffect, useRef } from 'react';
import { api } from '../lib/transport';
import { setState, useHud } from '../lib/store';
import { useDevelopment } from '../lib/development';
import { notify } from '../lib/notifications';
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
  const { scene } = useDevelopment();
  const metrics = useHud(s => s.metrics);
  const widgets = scene?.widgets.filter(w => w.binding !== 'development') || [];
  if (!scene || !widgets.length) return null;
  const revision = scene.revision;
  return <section className="visual-widgets" aria-label="Custom visuals" style={{ gridTemplateColumns: `repeat(${scene.layout.columns},minmax(0,1fr))` }}>
    {widgets.map(w => {
      const value = w.binding && w.binding !== 'development' ? metrics[w.binding] : w.value;
      return <article key={w.id} className="visual-widget"><label className="micro" htmlFor={`visual-${w.id}`}>{w.label}</label>
        {w.kind === 'chart' || w.kind === 'progress' ? <><Plot value={typeof value === 'number' ? value : undefined} series={w.kind === 'chart' ? w.series || [] : undefined} accent={scene.layout.accent} /><span className="text-meta t2">{w.kind === 'progress' ? `${value ?? '—'}%` : `${w.series?.length || 0} samples`}</span></> :
        w.kind === 'button' ? <button className="mini-btn" onClick={() => { if (w.action) setState({ drawer: w.action.drawer }); }}>{String(value ?? w.label)}</button> :
        w.kind === 'field' ? <input key={`${w.id}-${revision}`} id={`visual-${w.id}`} className="field w-full p-1 text-small" maxLength={300} defaultValue={String(value ?? '')} onBlur={e => {
          if (e.target.value !== String(value ?? '')) void api('/api/visual', { method: 'POST', body: { action: 'upsert', revision, widget: { ...w, value: e.target.value } } }).then(() => window.dispatchEvent(new Event('era:visual'))).catch(e => notify({ kind: 'warn', title: 'Visual update failed', message: String(e.message || e) }));
        }} /> : <p className="text-small t1 break-words">{String(value ?? '—')}</p>}
      </article>;
    })}
  </section>;
}
