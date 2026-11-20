import type { AgentState } from '../../lib/types';
import { glow, label } from './sprites';

// THE CONSTELLATION — Era's real tool registry as a node graph around the
// eclipse (after "AI Dashboard for Control AI Policy Platform", G. Railean):
// luminous curved spokes from the core to every capability, nodes with orbit
// rings, labels with the REAL tool count per category, traffic along spokes.
//
//   node size    tools registered in that category (/api/capabilities)
//   heat         the running tool's category = 1; recently used categories
//                glow, fading over 10 minutes (real tool.started timestamps)
//   traffic      routing → outward bursts to every node; tool_running →
//                pulses on the active spoke; idle → a slow inward heartbeat
//
// Cost: 3 batched strokes + a few hot spokes, ≤28 pulse blits, 2 blits per
// node and cached label bitmaps (no fillText per frame).

export interface CoreNode { id: string; count: number }

export interface ConstellationFrame {
  ctx: CanvasRenderingContext2D;
  cx: number; cy: number; R: number; w: number; h: number;
  light: boolean; inten: number; time: number; dt: number; motion: number;
  st: AgentState; nodes: CoreNode[]; active: string; lastUsed: Record<string, number>;
  labels: boolean; dpr: number; glowK: number;
}

const IDENTITY = [338, 268, 205, 42];
const MAXP = 28;
const RECENT_S = 600;
const RATE: Partial<Record<AgentState, number>> = {
  routing: 14, thinking: 4, tool_running: 8, speaking: 2, idle: 0.6, listening: 0.8, complete: 3,
};

export function createConstellation(rnd: () => number) {
  let key = '', ids = '';
  let px = new Float32Array(0), py = new Float32Array(0), ctrlX = new Float32Array(0), ctrlY = new Float32Array(0);
  let ax = new Float32Array(0), ay = new Float32Array(0), heat = new Float32Array(0), rad = new Float32Array(0);
  let compact = false;
  const pNode = new Int16Array(MAXP), pT = new Float32Array(MAXP), pV = new Float32Array(MAXP);
  const pDir = new Int8Array(MAXP), pLive = new Uint8Array(MAXP);
  let acc = 0;

  /** Positions on two facing arcs, clear of the state label (top) and transcript (bottom). */
  function layout(f: ConstellationFrame): void {
    const n = f.nodes.length;
    const nodeIds = f.nodes.map(x => x.id).join('|');
    const k = `${nodeIds}#${Math.round(f.w)}x${Math.round(f.h)}@${Math.round(f.R)}|${f.labels}|${Math.round(f.cx)}`;
    if (k === key) return;
    key = k;
    if (nodeIds !== ids) { ids = nodeIds; heat = new Float32Array(n); pLive.fill(0); }
    px = new Float32Array(n); py = new Float32Array(n); ctrlX = new Float32Array(n); ctrlY = new Float32Array(n);
    ax = new Float32Array(n); ay = new Float32Array(n); rad = new Float32Array(n);
    const rx = Math.min(f.R * 2.4, f.w / 2 - (f.labels ? 104 : 26));
    const ry = Math.min(f.R * 1.8, f.h / 2 - 28);
    compact = rx < f.R * 1.35;
    const half = Math.ceil(n / 2);
    for (let i = 0; i < n; i++) {
      const left = i < half, idx = left ? i : i - half, count = left ? half : n - half;
      const t = count <= 1 ? 0.5 : idx / (count - 1);
      const deg = -60 + t * 120;
      const th = ((left ? 180 - deg : deg) * Math.PI) / 180;
      const jitter = idx % 2 ? 0.88 : 1;
      const x = f.cx + Math.cos(th) * Math.max(f.R * 1.25, rx) * jitter;
      const y = f.cy + Math.sin(th) * Math.max(f.R * 1.1, ry) * jitter;
      px[i] = x; py[i] = y;
      // spoke anchor on the rim and a gently bent control point
      const dx = x - f.cx, dy = y - f.cy, d = Math.hypot(dx, dy) || 1;
      ax[i] = f.cx + (dx / d) * f.R * 0.92; ay[i] = f.cy + (dy / d) * f.R * 0.92;
      const mx = (ax[i] + x) / 2, my = (ay[i] + y) / 2, bend = (left ? -1 : 1) * 0.1 * d;
      ctrlX[i] = mx - (dy / d) * bend; ctrlY[i] = my + (dx / d) * bend;
      rad[i] = Math.min(9, 3 + Math.sqrt(Math.max(1, f.nodes[i].count)) * 1.25) * (f.h < 420 ? 0.8 : 1);
    }
  }

  const bez = (i: number, t: number): [number, number] => {
    const u = 1 - t;
    return [u * u * ax[i] + 2 * u * t * ctrlX[i] + t * t * px[i], u * u * ay[i] + 2 * u * t * ctrlY[i] + t * t * py[i]];
  };

  /** Behind the eclipse: links, spokes, traffic. */
  function back(f: ConstellationFrame): void {
    layout(f);
    const { ctx, dt, st } = f;
    const n = f.nodes.length;
    if (!n) return;
    const off = st === 'offline' || st === 'standby';
    const now = Date.now() / 1000;
    for (let i = 0; i < n; i++) {
      const id = f.nodes[i].id;
      let used = f.lastUsed[id] ?? 0;
      if (used > 1e12) used /= 1000;
      const age = now - used;
      const recent = used && age >= -5 && age < RECENT_S ? (1 - Math.max(0, age) / RECENT_S) ** 2 * 0.7 : 0;
      const target = st === 'tool_running' && id === f.active ? 1 : recent;
      heat[i] += (target - heat[i]) * (1 - Math.exp(-(target > heat[i] ? 6 : 1.2) * dt));
    }
    const dim = off ? 0.35 : 1;
    const base = f.light ? '20, 45, 90' : '150, 185, 255';

    // constellation links between neighbouring nodes
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${base}, ${Math.min(1, 0.07 * dim * f.glowK)})`;
    ctx.beginPath();
    const half = Math.ceil(n / 2);
    for (let i = 0; i < n - 1; i++) {
      if (i === half - 1) continue;
      ctx.moveTo(px[i], py[i]); ctx.lineTo(px[i + 1], py[i + 1]);
    }
    ctx.stroke();

    // every spoke, one stroke
    ctx.strokeStyle = `rgba(${base}, ${Math.min(1, (0.09 + f.inten * 0.05) * dim * f.glowK)})`;
    ctx.beginPath();
    for (let i = 0; i < n; i++) { ctx.moveTo(ax[i], ay[i]); ctx.quadraticCurveTo(ctrlX[i], ctrlY[i], px[i], py[i]); }
    ctx.stroke();
    // hot spokes in their node's identity colour
    for (let i = 0; i < n; i++) {
      if (heat[i] < 0.03) continue;
      ctx.lineWidth = 1 + heat[i] * 1.2;
      ctx.strokeStyle = `hsla(${IDENTITY[i % 4]}, 70%, ${f.light ? 40 : 72}%, ${Math.min(1, (0.1 + heat[i] * 0.6) * f.glowK)})`;
      ctx.beginPath(); ctx.moveTo(ax[i], ay[i]); ctx.quadraticCurveTo(ctrlX[i], ctrlY[i], px[i], py[i]); ctx.stroke();
    }
    ctx.lineWidth = 1;

    // traffic
    if (f.motion <= 0) { pLive.fill(0); acc = 0; }
    else if (!off) {
      acc += (RATE[st] ?? 0) * dt * f.motion;
      const activeIdx = st === 'tool_running' ? f.nodes.findIndex(x => x.id === f.active) : -1;
      for (let p = 0; p < MAXP && acc >= 1; p++) {
        if (pLive[p]) continue;
        acc -= 1;
        pNode[p] = activeIdx >= 0 ? activeIdx : Math.floor(rnd() * n);
        pT[p] = 0; pV[p] = (0.55 + rnd() * 0.7) * (st === 'idle' ? 0.45 : 1);
        pDir[p] = st === 'idle' || st === 'listening' ? -1 : 1; pLive[p] = 1;
      }
      if (acc > 1) acc = 1;
    } else { pLive.fill(0); acc = 0; }
    for (let p = 0; p < MAXP; p++) {
      if (!pLive[p]) continue;
      pT[p] += pV[p] * dt * f.motion;
      if (pT[p] >= 1 || pNode[p] >= n) { pLive[p] = 0; continue; }
      const t = pDir[p] === 1 ? pT[p] : 1 - pT[p];
      const [x, y] = bez(pNode[p], t);
      const s = 7 + heat[pNode[p]] * 5;
      ctx.globalAlpha = Math.sin(pT[p] * Math.PI) * 0.9;
      ctx.drawImage(glow(IDENTITY[pNode[p] % 4], 80, f.light, 96, 16), x - s / 2, y - s / 2, s, s);
    }
    ctx.globalAlpha = 1;
  }

  /** In front: nodes, orbit rings, satellites and labels. */
  function front(f: ConstellationFrame): void {
    const { ctx, time, st } = f;
    const n = f.nodes.length;
    if (!n || px.length !== n) return;
    const dim = st === 'offline' || st === 'standby' ? 0.45 : 1;
    // orbit rings, one stroke
    ctx.strokeStyle = f.light ? `rgba(40, 60, 110, ${0.3 * dim})` : `rgba(200, 190, 255, ${0.22 * dim})`;
    ctx.beginPath();
    for (let i = 0; i < n; i++) { const r = rad[i] * 1.9; ctx.moveTo(px[i] + r, py[i]); ctx.arc(px[i], py[i], r, 0, Math.PI * 2); }
    ctx.stroke();
    for (let i = 0; i < n; i++) {
      const hue = IDENTITY[i % 4], r = rad[i], h = heat[i];
      ctx.globalAlpha = Math.min(1, (0.22 + h * 0.6) * dim * f.glowK);
      const halo = r * (4.6 + h * 2);
      ctx.drawImage(glow(hue, 62, f.light, 70, 64), px[i] - halo / 2, py[i] - halo / 2, halo, halo);
      ctx.globalAlpha = Math.min(1, (0.75 + h * 0.25) * dim);
      const core = r * 2.3;
      ctx.drawImage(glow(hue, 55, f.light, h > 0.5 ? 97 : 88, 32), px[i] - core / 2, py[i] - core / 2, core, core);
      if (h > 0.25) {
        // a satellite orbiting the hot node, and a breathing outer ring
        const oa = time * 2.4 + i, orr = r * 2.7;
        ctx.globalAlpha = Math.min(1, h);
        ctx.drawImage(glow(hue, 70, f.light, 96, 16), px[i] + Math.cos(oa) * orr - 3, py[i] + Math.sin(oa) * orr - 3, 6, 6);
        ctx.strokeStyle = `hsla(${hue}, 70%, ${f.light ? 40 : 75}%, ${0.35 * h})`;
        ctx.beginPath(); ctx.arc(px[i], py[i], orr + Math.sin(time * 5) * 1.2, 0, Math.PI * 2); ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    if (!f.labels || compact) return;
    ctx.globalCompositeOperation = 'source-over';
    const nameColor = f.light ? '#1d2e45' : '#cdd8f6', countColor = f.light ? '#5a6b85' : '#7686b4';
    for (let i = 0; i < n; i++) {
      const left = px[i] < f.cx;
      const name = label(f.nodes[i].id.toUpperCase(), nameColor, f.dpr);
      const cnt = label(`${f.nodes[i].count}`, countColor, f.dpr);
      const nw = name.width / f.dpr, cw = cnt.width / f.dpr, lh = name.height / f.dpr;
      const gap = rad[i] * 2 + 8;
      const x0 = left ? px[i] - gap - nw - cw - 4 : px[i] + gap;
      ctx.globalAlpha = Math.min(1, (0.62 + heat[i] * 0.38) * dim);
      ctx.drawImage(name, x0, py[i] - lh / 2, nw, lh);
      ctx.globalAlpha = 0.85 * dim;
      ctx.drawImage(cnt, x0 + nw + 4, py[i] - lh / 2, cw, lh);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = f.light ? 'source-over' : 'lighter';
  }

  /** Node centre for tool beams (null until the first layout). */
  function position(id: string, nodes: CoreNode[]): [number, number] | null {
    const i = nodes.findIndex(n => n.id === id);
    return i >= 0 && i < px.length ? [px[i], py[i]] : null;
  }

  return { back, front, position };
}
