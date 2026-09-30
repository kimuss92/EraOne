import { memo, useEffect, useRef } from 'react';
import { live } from '../lib/store';
import type { AgentState } from '../lib/types';

// NEURAL CORE — the procedural sphere at the centre of the HUD: a fibonacci
// point-cloud shell, inner neuron nodes, synapse edges and travelling pulses,
// now driven by the NORMALIZED agent state (app/hud/events.py) instead of the
// old four-state activity. Every visual below maps to a real signal:
//
//   listening     inward ripples + brightness from the REAL mic RMS
//   speaking      the equalizer: 8 band energies of Era's REAL voice audio
//   tool_running  a directed pulse from the core to the tool's capability node
//                 and on toward the Live Activity panel (left), per tool start
//   routing       outward sweeps to the capability nodes
//   transcribing  a scanning arc; thinking: dense synapse traffic
//   complete / interrupted / wake / error: one-shot rings on entering the state
//   awaiting_approval: calm core + one slow breathing ring (static when motion
//                 is reduced) — the question itself is in text below the core
//
// Capability nodes come from the backend tool registry (/api/capabilities),
// so a new skill appears on the ring without a frontend change.
//
// PERFORMANCE: dots are pre-rendered glow SPRITES blitted with drawImage (the
// old per-dot arc+fill was the dominant cost); edges are batched into four
// alpha buckets = four strokes. Frame rate adapts: 60 fps while something is
// happening, 30 at rest, 20 asleep/offline, 0 when the tab is hidden. The
// measured rate is published to live.fps for the diagnostics drawer.

interface Props {
  state: AgentState;
  hue: number;
  sat: number;
  light: boolean;
  nodes: string[];
  activeCategory: string;
  toolPulse: number;
  reducedMotion: boolean;
}

interface Tuning {
  inten: number;
  rate: number;
  rot: number;
  speed: number;
}

const TUNE: Record<AgentState, Tuning> = {
  idle: { inten: 0.55, rate: 2.5, rot: 0.22, speed: 1 },
  wake_detected: { inten: 0.95, rate: 10, rot: 0.5, speed: 1.6 },
  listening: { inten: 0.68, rate: 5, rot: 0.28, speed: 1 },
  transcribing: { inten: 0.8, rate: 9, rot: 0.5, speed: 1.3 },
  routing: { inten: 0.85, rate: 12, rot: 0.4, speed: 1.5 },
  thinking: { inten: 0.9, rate: 17, rot: 0.34, speed: 1.6 },
  tool_running: { inten: 0.95, rate: 20, rot: 0.32, speed: 2 },
  synthesizing: { inten: 0.84, rate: 8, rot: 0.16, speed: 1.1 },
  speaking: { inten: 0.82, rate: 6, rot: 0.24, speed: 1 },
  interrupted: { inten: 0.6, rate: 2, rot: 0.2, speed: 0.8 },
  complete: { inten: 0.8, rate: 4, rot: 0.22, speed: 1 },
  error: { inten: 0.5, rate: 1.5, rot: 0.12, speed: 0.6 },
  offline: { inten: 0.12, rate: 0.3, rot: 0.06, speed: 0.35 },
  standby: { inten: 0.22, rate: 0.8, rot: 0.08, speed: 0.35 },
  awaiting_approval: { inten: 0.6, rate: 1.2, rot: 0.1, speed: 0.55 },
};

const BUSY = new Set<AgentState>([
  'wake_detected', 'listening', 'transcribing', 'routing', 'thinking',
  'tool_running', 'synthesizing', 'speaking', 'interrupted', 'complete',
]);

const CORTEX = 1100;
const NEURONS = 200;
const LINKS = 3;
const EQ = 8;
const EQ_DISPLACE = 0.22;
const SPARK_HUE = 34;

function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function geometry() {
  const rnd = mulberry32(1337);
  const fib = (n: number, off: number, inset: number) => {
    const x = new Float32Array(n), y = new Float32Array(n), z = new Float32Array(n), band = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const t = (i + off) / n;
      const th = Math.acos(1 - 2 * t);
      const ph = (i + off) * Math.PI * (3 - Math.sqrt(5)) + (off > 0.5 ? 1.7 : 0);
      const r = (1 + (rnd() - 0.5) * 0.03) * inset;
      x[i] = Math.sin(th) * Math.cos(ph) * r;
      y[i] = Math.cos(th) * r;
      z[i] = Math.sin(th) * Math.sin(ph) * r;
      band[i] = ((Math.cos(th) + 1) / 2) * (EQ - 1);
    }
    return { x, y, z, band };
  };
  const shell = fib(CORTEX, 0.5, 1);
  const neu = fib(NEURONS, 0.73, 0.92);
  const seen = new Set<number>();
  const ea: number[] = [], eb: number[] = [];
  for (let i = 0; i < NEURONS; i++) {
    const d: [number, number][] = [];
    for (let j = 0; j < NEURONS; j++) {
      if (j === i) continue;
      const dx = neu.x[i] - neu.x[j], dy = neu.y[i] - neu.y[j], dz = neu.z[i] - neu.z[j];
      d.push([j, dx * dx + dy * dy + dz * dz]);
    }
    d.sort((p, q) => p[1] - q[1]);
    for (let k = 0; k < LINKS; k++) {
      const j = d[k][0];
      const key = i < j ? i * 4096 + j : j * 4096 + i;
      if (!seen.has(key)) { seen.add(key); ea.push(Math.min(i, j)); eb.push(Math.max(i, j)); }
    }
  }
  return { shell, neu, ea: Int32Array.from(ea), eb: Int32Array.from(eb) };
}

const spriteCache = new Map<string, HTMLCanvasElement>();
function sprite(h: number, s: number, light: boolean, hot = 92): HTMLCanvasElement {
  const key = `${Math.round(h / 3) * 3}|${Math.round(s / 5) * 5}|${light ? 1 : 0}|${hot}`;
  let c = spriteCache.get(key);
  if (c) return c;
  c = document.createElement('canvas');
  c.width = c.height = 32;
  const g = c.getContext('2d')!;
  const grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  const l = light ? 32 : 62;
  grad.addColorStop(0, `hsla(${h}, ${Math.min(100, s + 10)}%, ${light ? 28 : hot}%, 1)`);
  grad.addColorStop(0.28, `hsla(${h}, ${s}%, ${l}%, 0.85)`);
  grad.addColorStop(0.55, `hsla(${h}, ${s}%, ${l}%, 0.22)`);
  grad.addColorStop(1, `hsla(${h}, ${s}%, ${l}%, 0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, 32, 32);
  if (spriteCache.size > 240) spriteCache.clear();
  spriteCache.set(key, c);
  return c;
}

function NeuralCore(props: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const p = useRef(props);
  p.current = props;

  useEffect(() => {
    const canvas = canvasRef.current!, box = boxRef.current!;
    const ctx = canvas.getContext('2d', { alpha: true })!;
    const geo = geometry();
    const nE = geo.ea.length;
    const pnx = new Float32Array(NEURONS), pny = new Float32Array(NEURONS), pnd = new Float32Array(NEURONS);
    const MAXP = 44;
    const pEdge = new Int32Array(MAXP), pT = new Float32Array(MAXP), pSpeed = new Float32Array(MAXP);
    const pDir = new Int8Array(MAXP), pLive = new Uint8Array(MAXP);
    const rings: { t: number; kind: 'out' | 'in' | 'burst'; life: number }[] = [];
    const beams: { t: number; node: number }[] = [];
    const eq = new Float32Array(EQ);
    const rnd = mulberry32(42);
    let raf = 0, w = 0, h = 0, dpr = 1, last = performance.now(), time = 0;
    let inten = 0.3, curHue = p.current.hue, curSat = p.current.sat, scale = 1, rotY = 0;
    let lastState: AgentState = p.current.state, lastPulse = p.current.toolPulse;
    let frames = 0, fpsAt = performance.now(), mic = 0, ripAcc = 0;

    const approach = (c: number, t: number, r: number, dt: number) => c + (t - c) * (1 - Math.exp(-r * dt));
    const resize = () => {
      const r = box.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 1.75);
      w = r.width; h = r.height;
      canvas.width = Math.max(1, Math.floor(w * dpr));
      canvas.height = Math.max(1, Math.floor(h * dpr));
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      last = 0; // resizing cleared the bitmap: draw on the very next frame
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(box);

    const render = () => {
      raf = requestAnimationFrame(render);
      if (document.hidden) return;
      const P = p.current;
      const st = P.state;
      const now = performance.now();
      const busy = BUSY.has(st) || live.micLevel > 0.02;
      const interval = st === 'standby' || st === 'offline' ? 50 : busy ? 1000 / 60 : 1000 / 30;
      if (now - last < interval - 2) return;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      frames++;
      if (now - fpsAt >= 1000) { live.fps = Math.round((frames * 1000) / (now - fpsAt)); frames = 0; fpsAt = now; }
      const motion = P.reducedMotion ? 0.4 : 1;
      time += dt * motion;

      // one-shot effects on entering a state
      if (st !== lastState) {
        if (st === 'complete') rings.push({ t: 0, kind: 'out', life: 1.2 });
        if (st === 'interrupted') rings.push({ t: 0, kind: 'in', life: 0.7 });
        if (st === 'wake_detected') { rings.push({ t: 0, kind: 'burst', life: 0.8 }); rings.push({ t: -0.18, kind: 'burst', life: 0.8 }); }
        if (st === 'error') rings.push({ t: 0, kind: 'in', life: 0.9 });
        lastState = st;
      }
      if (P.toolPulse !== lastPulse) {
        lastPulse = P.toolPulse;
        beams.push({ t: 0, node: P.nodes.indexOf(P.activeCategory) });
      }

      const T = TUNE[st];
      const amp = st === 'speaking' ? live.amplitude : 0;
      mic = approach(mic, Math.min(1, Math.sqrt(live.micLevel) * 2.2), 14, dt);
      const listenBoost = st === 'listening' || st === 'transcribing' || st === 'idle' ? mic * 0.35 : 0;
      inten = approach(inten, T.inten + amp * 0.5 + listenBoost + Math.sin(time * 1.3) * 0.04, 3, dt);
      // Hue glide. A big swing (cyan -> gold) would pass through green on the
      // way, so saturation dips while the hue is far from its target: the core
      // cools to neutral, then warms into the new colour.
      const dh = ((P.hue - curHue + 540) % 360) - 180;
      curHue += dh * (1 - Math.exp(-(Math.abs(dh) > 50 ? 3.2 : 1.6) * dt));
      const swing = Math.min(1, Math.abs(dh) / 90);
      curSat = approach(curSat, P.sat * (1 - 0.85 * swing), swing > 0.3 ? 8 : 1.6, dt);
      scale = approach(scale, st === 'standby' ? 0.5 : st === 'offline' ? 0.82 : 1, 1.6, dt);
      rotY += dt * T.rot * motion;

      for (let b = 0; b < EQ; b++) {
        const target = st === 'speaking' ? live.bands[b] ?? 0 : 0;
        eq[b] = approach(eq[b], target, target > eq[b] ? 26 : 7, dt);
      }
      const eqAt = (bf: number) => { const i = bf | 0; const a = eq[i] ?? 0; const bb = eq[i + 1] ?? a; return a + (bb - a) * (bf - i); };
      const eqOn = eq.some(v => v > 0.002);

      // spawn synapse pulses at the state's firing rate
      let want = (T.rate + amp * 24 + mic * 6) * dt * motion;
      for (let i = 0; i < MAXP && want > 0; i++) {
        if (pLive[i]) continue;
        if (rnd() < want) { pLive[i] = 1; pEdge[i] = Math.floor(rnd() * nE); pT[i] = 0; pSpeed[i] = (0.9 + rnd() * 1.4) * T.speed; pDir[i] = rnd() < 0.5 ? -1 : 1; }
        want -= 1;
      }

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      // Centre sits a little above the middle: the state label lives below it.
      const cx = w / 2, cy = h / 2 - Math.min(18, h * 0.025);
      // The core stays the hero on large displays: the cap grows with the
      // stage (240 px at 1080p, ~330 px at 1440p) instead of a fixed 240.
      const rCap = w < 880 ? 170 : Math.max(240, Math.min(340, Math.min(w, h) * 0.26));
      const R = Math.max(40, Math.min(rCap, (Math.min(w, h) / 2 - 38) / 1.62)) * scale;
      const cY = Math.cos(rotY), sY = Math.sin(rotY);
      const rotX = -0.2 + Math.sin(time * 0.23) * 0.08;
      const cX = Math.cos(rotX), sX = Math.sin(rotX);
      const fov = 3.2;
      let ox = 0, oy = 0, od = 0;
      const proj = (x: number, y: number, z: number, sw: number) => {
        x *= sw; y *= sw; z *= sw;
        const x2 = x * cY - z * sY, z2 = x * sY + z * cY;
        const y2 = y * cX - z2 * sX, z3 = y * sX + z2 * cX;
        const s = fov / (fov + z3);
        ox = cx + x2 * R * s; oy = cy - y2 * R * s; od = (z3 + 1.15) / 2.3;
      };
      const light = P.light;
      ctx.globalCompositeOperation = light ? 'source-over' : 'lighter';

      // volumetric glow
      const halo = ctx.createRadialGradient(cx, cy, R * 0.05, cx, cy, R * 1.6);
      const ga = (light ? 0.1 : 0.3) * (0.3 + inten);
      halo.addColorStop(0, `hsla(${curHue}, ${curSat}%, ${light ? 45 : 62}%, ${ga})`);
      halo.addColorStop(0.45, `hsla(${curHue}, ${curSat}%, 55%, ${ga * 0.33})`);
      halo.addColorStop(1, `hsla(${curHue}, ${curSat}%, 50%, 0)`);
      ctx.fillStyle = halo;
      ctx.fillRect(cx - R * 1.6, cy - R * 1.6, R * 3.2, R * 3.2);

      // neurons projected once (edges + nodes + pulses share them)
      for (let i = 0; i < NEURONS; i++) {
        proj(geo.neu.x[i], geo.neu.y[i], geo.neu.z[i], eqOn ? 1 + eqAt(geo.neu.band[i]) * EQ_DISPLACE : 1);
        pnx[i] = ox; pny[i] = oy; pnd[i] = od;
      }

      // synapses: 4 alpha buckets = 4 strokes
      ctx.lineWidth = 1;
      for (let b = 0; b < 4; b++) {
        ctx.beginPath();
        for (let e = 0; e < nE; e++) {
          const a = geo.ea[e], bb = geo.eb[e];
          const d = (pnd[a] + pnd[bb]) / 2;
          if (Math.min(3, Math.floor((1 - d) * 4)) !== b) continue;
          ctx.moveTo(pnx[a], pny[a]); ctx.lineTo(pnx[bb], pny[bb]);
        }
        const alpha = (0.07 + b * 0.075) * (0.5 + inten);
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, ${light ? 35 : 66}%, ${alpha})`;
        ctx.stroke();
      }

      // shell cloud (sprites)
      const dot = sprite(curHue, curSat, light);
      for (let i = 0; i < CORTEX; i++) {
        const e = eqOn ? eqAt(geo.shell.band[i]) : 0;
        proj(geo.shell.x[i], geo.shell.y[i], geo.shell.z[i], 1 + e * EQ_DISPLACE);
        const near = 1 - od;
        const r = (0.8 + near * 1.3 + inten * 0.6 + e * 1.1) * 2.3;
        ctx.globalAlpha = Math.min(1, 0.16 + near * 0.7 * (0.5 + inten * 0.8) + e * 0.35);
        ctx.drawImage(dot, ox - r, oy - r, r * 2, r * 2);
      }

      // neuron nodes (+ gold computation sparks while thinking/executing)
      const spark = sprite(SPARK_HUE, 95, light, 88);
      const sparkBoost = st === 'thinking' || st === 'tool_running' ? 1.6 : 1;
      for (let i = 0; i < NEURONS; i++) {
        const near = 1 - pnd[i];
        const isSpark = st !== 'offline' && i % 14 === 0;
        const flare = isSpark ? Math.max(0, Math.sin(time * 1.5 + i * 1.3)) ** 3 * sparkBoost : 0;
        const tw = 0.7 + 0.3 * Math.sin(time * 2.1 + i * 2.7);
        const r = (1.3 + near * 1.5 + inten + flare * 1.6) * 2.6;
        ctx.globalAlpha = Math.min(1, (0.3 + near * 0.6) * tw * (0.5 + inten * 0.9) + flare * 0.5);
        ctx.drawImage(flare > 0.15 ? spark : dot, pnx[i] - r, pny[i] - r, r * 2, r * 2);
      }

      // travelling pulses
      const hot = sprite(curHue, curSat, light, 97);
      for (let i = 0; i < MAXP; i++) {
        if (!pLive[i]) continue;
        pT[i] += pSpeed[i] * dt * motion;
        if (pT[i] >= 1) { pLive[i] = 0; continue; }
        const a = geo.ea[pEdge[i]], b = geo.eb[pEdge[i]];
        const t = pDir[i] === 1 ? pT[i] : 1 - pT[i];
        const x = pnx[a] + (pnx[b] - pnx[a]) * t, y = pny[a] + (pny[b] - pny[a]) * t;
        const fade = Math.sin(pT[i] * Math.PI);
        const r = 7 + amp * 5;
        ctx.globalAlpha = fade;
        ctx.drawImage(hot, x - r, y - r, r * 2, r * 2);
      }
      ctx.globalAlpha = 1;

      // capability ring: nodes from the real tool registry
      const nodes = P.nodes;
      const nR = R / scale * 1.5;
      const nodePos: [number, number][] = nodes.map((_, i) => {
        const ang = -Math.PI / 2 + (i / Math.max(1, nodes.length)) * Math.PI * 2 + 0.2;
        return [cx + Math.cos(ang) * nR, cy + Math.sin(ang) * nR * 0.9];
      });
      if (st !== 'standby' && st !== 'offline' && nodes.length) {
        ctx.font = '500 9px "JetBrains Mono Variable", monospace';
        ctx.textAlign = 'center';
        nodes.forEach((name, i) => {
          const [x, y] = nodePos[i];
          const active = st === 'tool_running' && name === P.activeCategory;
          const routing = st === 'routing';
          if (routing) {
            // outward sweep: a bright dash runs from the core edge to each node
            const ph = (time * 1.6 + i / nodes.length) % 1;
            const sx = cx + (x - cx) * (0.62 + ph * 0.38), sy = cy + (y - cy) * (0.62 + ph * 0.38);
            ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, 70%, ${0.12 + 0.3 * Math.sin(ph * Math.PI)})`;
            ctx.beginPath(); ctx.moveTo(cx + (x - cx) * 0.62, cy + (y - cy) * 0.62); ctx.lineTo(sx, sy); ctx.stroke();
          }
          if (active) {
            ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, 72%, 0.45)`;
            ctx.beginPath(); ctx.moveTo(cx + (x - cx) * 0.62, cy + (y - cy) * 0.62); ctx.lineTo(x, y); ctx.stroke();
          }
          const r = active ? 11 + Math.sin(time * 8) * 2 : 5;
          ctx.globalAlpha = active ? 1 : 0.55;
          ctx.drawImage(active ? hot : dot, x - r, y - r, r * 2, r * 2);
          ctx.globalAlpha = active ? 0.95 : 0.4;
          ctx.fillStyle = light ? '#1d4a5a' : `hsl(${curHue}, 40%, 78%)`;
          ctx.fillText(name.toUpperCase(), x, y + (y >= cy ? 18 : -12));
        });
        ctx.globalAlpha = 1;
      }

      // tool beams: core → capability node → out toward Live Activity (left)
      for (let i = beams.length - 1; i >= 0; i--) {
        const bm = beams[i];
        bm.t += dt * 1.1;
        if (bm.t >= 1) { beams.splice(i, 1); continue; }
        const [nx, ny] = bm.node >= 0 ? nodePos[bm.node] : [cx - nR, cy];
        const pts: [number, number][] = [[cx, cy], [nx, ny], [0, ny + (cy - ny) * 0.4]];
        const seg = bm.t < 0.45 ? 0 : 1;
        const lt = seg === 0 ? bm.t / 0.45 : (bm.t - 0.45) / 0.55;
        const [ax, ay] = pts[seg], [bx, by] = pts[seg + 1];
        const x = ax + (bx - ax) * lt, y = ay + (by - ay) * lt;
        const tail = Math.max(0, lt - 0.18);
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, 78%, ${0.55 * (1 - bm.t * 0.6)})`;
        ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(ax + (bx - ax) * tail, ay + (by - ay) * tail); ctx.lineTo(x, y); ctx.stroke();
        ctx.lineWidth = 1;
        ctx.drawImage(hot, x - 9, y - 9, 18, 18);
      }

      // state rings
      const ringStroke = (rad: number, a: number, width = 1) => {
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, ${light ? 38 : 66}%, ${a})`;
        ctx.lineWidth = width;
        ctx.beginPath(); ctx.ellipse(cx, cy, rad, rad * 0.95, 0, 0, Math.PI * 2); ctx.stroke();
        ctx.lineWidth = 1;
      };
      if (!P.reducedMotion) {
        // listening: inward waves whose rate follows the real mic level
        if (st === 'listening' || st === 'transcribing') {
          ripAcc += dt * (0.6 + mic * 3);
          if (ripAcc > 1) { ripAcc = 0; rings.push({ t: 0, kind: 'in', life: 1.6 }); }
        }
        // speaking: outward waves from Era's real voice amplitude
        if (st === 'speaking') {
          ripAcc += dt * (0.8 + amp * 3.5);
          if (ripAcc > 1) { ripAcc = 0; rings.push({ t: 0, kind: 'out', life: 1.8 }); }
        }
      }
      for (let i = rings.length - 1; i >= 0; i--) {
        const rg = rings[i];
        rg.t += dt;
        if (rg.t >= rg.life) { rings.splice(i, 1); continue; }
        if (rg.t < 0) continue;
        const q = rg.t / rg.life;
        const a = Math.sin(q * Math.PI) * (rg.kind === 'burst' ? 0.5 : 0.22) * (0.5 + inten);
        const rad = rg.kind === 'in' ? R * (1.6 - q * 0.95) : rg.kind === 'burst' ? R * (0.9 + q * 1.1) : R * (0.6 + q * 1.1);
        ringStroke(rad, a, rg.kind === 'burst' ? 2 : 1);
      }
      if (st === 'transcribing') {
        const a0 = time * 3.2;
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, 74%, 0.5)`;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.ellipse(cx, cy, R * 1.14, R * 1.08, 0, a0, a0 + Math.PI / 2); ctx.stroke();
        ctx.lineWidth = 1;
      }
      if (st === 'synthesizing') ringStroke(R * (1.25 - ((time * 0.5) % 1) * 0.18), 0.28);
      if (st === 'thinking') ringStroke(R * 1.18, 0.1 + 0.06 * Math.sin(time * 3));
      // approval: a slow, deliberate breath (~0.3 Hz), never a flash
      if (st === 'awaiting_approval') {
        const breath = P.reducedMotion ? 0.5 : 0.5 + 0.5 * Math.sin(time * 1.9);
        ringStroke(R * (1.16 + breath * 0.04), 0.14 + breath * 0.2, 1.5);
      }
    };
    raf = requestAnimationFrame(render);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);

  return (
    <div ref={boxRef} className="pointer-events-none absolute inset-0">
      <canvas ref={canvasRef} className="block h-full w-full" />
    </div>
  );
}

// Props change at conversation pace; the animation reads live refs per frame.
export default memo(NeuralCore);
