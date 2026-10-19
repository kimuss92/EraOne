import { memo, useEffect, useRef, type CSSProperties } from 'react';
import { history, useTelemetryVersion, type Sample } from '../lib/telemetry';
import { PRESETS, useLook } from '../lib/appearance';

// Live CPU / GPU / RAM graph from the REAL backend samples (lib/telemetry.ts).
// Redrawn only when a new sample arrives or the box resizes — never per frame,
// so it costs nothing between metric updates. Gaps (null) break the line.

type Key = 'cpu' | 'gpu' | 'ram';
type Hsl = [number, number, number];

function series(accent: number): { key: Key; label: string; hsl: Hsl }[] {
  return [
    { key: 'cpu', label: 'CPU', hsl: [accent, 90, 72] },
    { key: 'gpu', label: 'GPU', hsl: [262, 90, 78] },
    { key: 'ram', label: 'RAM', hsl: [330, 80, 78] },
  ];
}
const hsla = ([h, s, l]: Hsl, a = 1) => `hsla(${h}, ${s}%, ${l}%, ${a})`;

function draw(canvas: HTMLCanvasElement, data: Sample[], accent: number) {
  const box = canvas.parentElement!;
  const w = box.clientWidth, h = box.clientHeight;
  if (!w || !h) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d')!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const light = document.documentElement.dataset.theme === 'light';
  ctx.strokeStyle = light ? 'rgba(20,50,90,.12)' : 'rgba(150,180,255,.09)';
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 4]);
  ctx.beginPath();
  for (const g of [0.25, 0.5, 0.75]) { const y = Math.round(h * g) + 0.5; ctx.moveTo(0, y); ctx.lineTo(w, y); }
  ctx.stroke();
  ctx.setLineDash([]);
  const n = data.length;
  if (n < 2) return;
  const step = w / (n - 1);
  const y = (v: number) => h - 2 - (Math.max(0, Math.min(100, v)) / 100) * (h - 4);
  for (const s of series(accent)) {
    const col = light ? ([s.hsl[0], s.hsl[1], 40] as Hsl) : s.hsl;
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, hsla(col, light ? 0.16 : 0.22));
    grad.addColorStop(1, hsla(col, 0));
    let run: number[] = [];
    const flush = () => {
      if (run.length >= 2) {
        ctx.beginPath();
        run.forEach((i, k) => { const v = data[i][s.key] as number; if (k) ctx.lineTo(i * step, y(v)); else ctx.moveTo(i * step, y(v)); });
        ctx.strokeStyle = hsla(col); ctx.lineWidth = 1.4; ctx.stroke();
        ctx.lineTo(run[run.length - 1] * step, h); ctx.lineTo(run[0] * step, h); ctx.closePath();
        ctx.fillStyle = grad; ctx.fill();
      }
      run = [];
    };
    for (let i = 0; i < n; i++) { if (data[i][s.key] === null) flush(); else run.push(i); }
    flush();
    const lv = data[n - 1][s.key];
    if (lv !== null) {
      ctx.fillStyle = hsla(col);
      ctx.shadowColor = hsla(col); ctx.shadowBlur = 6;
      ctx.beginPath(); ctx.arc((n - 1) * step - 2, y(lv), 2, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
    }
  }
}

function TelemetryGraph() {
  const version = useTelemetryVersion();
  const look = useLook();
  const accent = PRESETS[look.preset].accent;
  const ref = useRef<HTMLCanvasElement>(null);
  const accentRef = useRef(accent);
  accentRef.current = accent;
  useEffect(() => { if (ref.current) draw(ref.current, history, accent); }, [version, accent]);
  useEffect(() => {
    const c = ref.current!;
    const ro = new ResizeObserver(() => draw(c, history, accentRef.current));
    ro.observe(c.parentElement!);
    return () => ro.disconnect();
  }, []);
  const last = history[history.length - 1];
  const span = history.length > 1 ? Math.round((history[history.length - 1].t - history[0].t) / 60000) : 0;
  return <div className="era-telemetry">
    <div className="era-telemetry-legend">
      {series(accent).map(s => <span key={s.key} style={{ '--dot': hsla(s.hsl) } as CSSProperties}>
        <i />{s.label}<b>{last && last[s.key] !== null ? `${Math.round(last[s.key] as number)}%` : 'N/A'}</b>
      </span>)}
      <em>{span ? `last ${span} min` : 'live'}</em>
    </div>
    <div className="era-telemetry-plot"><canvas ref={ref} role="img" aria-label="CPU, GPU and RAM usage over time" /></div>
  </div>;
}

export default memo(TelemetryGraph);
