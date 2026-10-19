import { memo, useEffect, useRef } from 'react';
import { live } from '../lib/store';
import { look } from '../lib/appearance';
import type { AgentState } from '../lib/types';
import { beginFrame, glow } from './core/sprites';
import { createEclipse } from './core/eclipse';
import { createConstellation, type CoreNode } from './core/constellation';
import { canvasInterval } from '../lib/cinematic';
import { createCanvasLoop } from '../lib/canvasLoop';

// NEURAL CORE — the canvas at the centre of the HUD. Three layers:
//   • the CONSTELLATION: Era's real tool registry as a node graph (core/constellation.ts)
//   • the ECLIPSE: Era's voice orb — void, luminous ring, fibres (core/eclipse.ts)
//   • the inner NETWORK: neurons, synapses and travelling pulses inside the void
// driven by the NORMALIZED agent state (app/hud/events.py). Every visual maps
// to a real signal:
//
//   listening     inward ripples + fibre reach from the REAL mic RMS
//   speaking      fibres + ring breath from Era's REAL voice audio (8 bands)
//   tool_running  a beam from the core to the tool's capability node and on
//                 toward Live Activity; that node + spoke glow while it runs
//   routing       the ring tilts, traffic bursts out along every spoke
//   thinking      dense synapse traffic, tilted ring, light streaks
//   complete / interrupted / wake / error: one-shot rings on entering the state
//   awaiting_approval: calm core + one slow breathing ring
//
// PERFORMANCE: everything costly is a cached sprite (core/sprites.ts); strokes
// are batched. ~450 blits/frame at medium density vs ~1350 for the old
// 1100-dot sphere. Frame rate adapts: 60 fps while something is happening,
// 30 at rest, 20 asleep/offline, 0 when the tab is hidden. Measured rate and
// CPU time go to live.fps / live.frameMs (Diagnostics drawer).

interface Props {
  hero?: boolean;
  paused?: boolean;
  state: AgentState;
  hue: number;
  sat: number;
  light: boolean;
  nodes: CoreNode[];
  lastUsed: Record<string, number>;
  activeCategory: string;
  toolPulse: number;
  reducedMotion: boolean;
  focused: boolean;
}

interface Tuning { inten: number; rate: number; rot: number; speed: number }

const TUNE: Record<AgentState, Tuning> = {
  idle: { inten: 0.55, rate: 2.5, rot: 0.34, speed: 1 },
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
  standby: { inten: 0.22, rate: 0.8, rot: 0.16, speed: 0.35 },
  awaiting_approval: { inten: 0.6, rate: 1.2, rot: 0.1, speed: 0.55 },
};

const BUSY = new Set<AgentState>([
  'wake_detected', 'listening', 'transcribing', 'routing', 'thinking',
  'tool_running', 'synthesizing', 'speaking', 'interrupted', 'complete',
]);

const NEURONS = 200;
const NEURON_COUNT = { low: 110, medium: 170, high: 200 } as const;
const LINKS = 3;
const EQ = 8;

function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Inner network: fibonacci-distributed neurons, each linked to its nearest three. */
function network() {
  const rnd = mulberry32(1337);
  const x = new Float32Array(NEURONS), y = new Float32Array(NEURONS), z = new Float32Array(NEURONS);
  for (let i = 0; i < NEURONS; i++) {
    const t = (i + 0.73) / NEURONS;
    const th = Math.acos(1 - 2 * t);
    const ph = (i + 0.73) * Math.PI * (3 - Math.sqrt(5)) + 1.7;
    const r = 0.55 + rnd() * 0.45; // a volume, not a shell: the network fills the void
    x[i] = Math.sin(th) * Math.cos(ph) * r; y[i] = Math.cos(th) * r; z[i] = Math.sin(th) * Math.sin(ph) * r;
  }
  const seen = new Set<number>(), ea: number[] = [], eb: number[] = [];
  for (let i = 0; i < NEURONS; i++) {
    const d: [number, number][] = [];
    for (let j = 0; j < NEURONS; j++) {
      if (j === i) continue;
      const dx = x[i] - x[j], dy = y[i] - y[j], dz = z[i] - z[j];
      d.push([j, dx * dx + dy * dy + dz * dz]);
    }
    d.sort((p, q) => p[1] - q[1]);
    for (let k = 0; k < LINKS; k++) {
      const j = d[k][0], key = i < j ? i * 4096 + j : j * 4096 + i;
      if (!seen.has(key)) { seen.add(key); ea.push(Math.min(i, j)); eb.push(Math.max(i, j)); }
    }
  }
  return { x, y, z, ea: Int32Array.from(ea), eb: Int32Array.from(eb) };
}

function NeuralCore(props: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const p = useRef(props);
  const syncRef = useRef<(() => void) | null>(null);
  p.current = props;

  useEffect(() => {
    const canvas = canvasRef.current!, box = boxRef.current!;
    const ctx = canvas.getContext('2d', { alpha: true })!;
    const net = network();
    const nE = net.ea.length;
    const pnx = new Float32Array(NEURONS), pny = new Float32Array(NEURONS), pnd = new Float32Array(NEURONS);
    const MAXP = 36;
    const pEdge = new Int32Array(MAXP), pT = new Float32Array(MAXP), pSpeed = new Float32Array(MAXP);
    const pDir = new Int8Array(MAXP), pLive = new Uint8Array(MAXP);
    const rings: { t: number; kind: 'out' | 'in' | 'burst'; life: number }[] = [];
    const beams: { t: number; node: string }[] = [];
    const eq = new Float32Array(EQ);
    const rnd = mulberry32(42);
    const eclipse = createEclipse(mulberry32(7));
    const constellation = createConstellation(mulberry32(99));
    let w = 0, h = 0, dpr = 1, last = performance.now(), time = 0;
    let refresh = () => {};
    let inten = 0.3, curHue = p.current.hue, curSat = p.current.sat, scale = 1, rotY = 0;
    let lastState: AgentState = p.current.state, lastPulse = p.current.toolPulse;
    let frames = 0, fpsAt = performance.now(), mic = 0, ripAcc = 0, focus = 0, hover = 0;

    const approach = (c: number, t: number, r: number, dt: number) => c + (t - c) * (1 - Math.exp(-r * dt));
    const eqAt = (bf: number) => { const i = bf | 0; const a = eq[i] ?? 0; const b = eq[i + 1] ?? a; return a + (b - a) * (bf - i); };
    const resize = () => {
      // Layout pixels (unaffected by the HUD-scale zoom); CSS sizes the canvas
      // element, the bitmap follows the zoomed device pixels (cap 1.75x / 2x).
      dpr = Math.min(Math.min(window.devicePixelRatio || 1, 1.75) * look.uiScale, 2);
      w = box.clientWidth; h = box.clientHeight;
      canvas.width = Math.max(1, Math.floor(w * dpr));
      canvas.height = Math.max(1, Math.floor(h * dpr));
      last = 0; // resizing cleared the bitmap: draw on the very next frame
      refresh();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(box);

    function render(now: number) {
      const P = p.current;
      const st = P.state;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const frameStart = performance.now();
      frames++;
      if (now - fpsAt >= 1000) { live.fps = Math.round((frames * 1000) / (now - fpsAt)); frames = 0; fpsAt = now; }
      beginFrame();
      const reduced = P.reducedMotion;
      const motion = reduced ? 0 : look.speed;
      if (reduced) { beams.length = 0; rings.length = 0; pLive.fill(0); eclipse.clear(); }
      time += dt * motion;

      // one-shot effects on entering a state
      if (st !== lastState && !reduced) {
        if (st === 'complete') rings.push({ t: 0, kind: 'out', life: 1.2 });
        if (st === 'interrupted') rings.push({ t: 0, kind: 'in', life: 0.7 });
        if (st === 'wake_detected') { rings.push({ t: 0, kind: 'burst', life: 0.8 }); rings.push({ t: -0.18, kind: 'burst', life: 0.8 }); }
        if (st === 'error') rings.push({ t: 0, kind: 'in', life: 0.9 });
      }
      lastState = st;
      if (P.toolPulse !== lastPulse) {
        lastPulse = P.toolPulse;
        if (!reduced) beams.push({ t: 0, node: P.activeCategory });
      }

      const T = TUNE[st];
      // Era's real output level, whatever the state label says this frame.
      const amp = live.amplitude;
      mic = approach(mic, Math.min(1, Math.sqrt(live.micLevel) * 2.2), 14, dt);
      const listenBoost = st === 'listening' || st === 'transcribing' || st === 'idle' ? mic * 0.35 : 0;
      inten = approach(inten, T.inten + amp * 0.5 + listenBoost + Math.sin(time * 1.3) * 0.04, 3, dt);
      // Hue glide. A big swing would pass through unrelated hues, so saturation
      // dips while the hue is far from its target: cool to neutral, then warm.
      const dh = ((P.hue - curHue + 540) % 360) - 180;
      curHue += dh * (1 - Math.exp(-(Math.abs(dh) > 50 ? 3.2 : 1.6) * dt));
      const swing = Math.min(1, Math.abs(dh) / 90);
      curSat = approach(curSat, P.sat * (1 - 0.85 * swing), swing > 0.3 ? 8 : 1.6, dt);
      scale = approach(scale, st === 'standby' ? 0.84 : st === 'offline' ? 0.82 : 1, 1.6, dt);
      rotY += dt * T.rot * motion;

      for (let b = 0; b < EQ; b++) {
        const target = amp > 0.002 ? live.bands[b] ?? 0 : 0;
        // snappy attack so a syllable lands on its frame; slower release so it reads
        eq[b] = approach(eq[b], target, target > eq[b] ? 45 : 11, dt);
      }
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
      focus = reduced ? Number(P.focused) : approach(focus, Number(P.focused), 5, dt);
      hover = approach(hover,reduced ? 0 : Number(box.parentElement?.dataset.pointerNear === 'true'),5,dt);
      const cx = w / 2 - focus * Math.min(22, w * 0.035), cy = h / 2 - Math.min(10, h * 0.015);
      const base = P.hero ? Math.min(w, h) * 0.235 : w < 880 ? 118 : Math.max(150, Math.min(250, Math.min(w, h) * 0.19));
      const R = Math.max(22, Math.min(base * look.coreScale, Math.min(w, h) / 2.5)) * scale * (1 - focus * 0.06);
      const light = P.light;
      ctx.globalCompositeOperation = light ? 'source-over' : 'lighter';
      const nCount = NEURON_COUNT[look.density];
      const ef = {
        ctx, cx, cy, R: R * (reduced ? 1 : 1.01 + Math.sin(time * 1.15) * .01), hue: curHue, sat: curSat, light, inten: inten + hover * .15, time, dt, motion, st, amp, mic,
        eqAt, eqOn, eqBands: EQ, glowK: look.glow * (1+hover*.16), density: look.density,
      };
      const cf = {
        ctx, cx, cy, R, w, h, light, inten: inten + hover * .3, time, dt, motion, st, nodes: P.nodes, active: P.activeCategory,
        lastUsed: P.lastUsed, labels: look.labels, dpr, glowK: look.glow * (1+hover*.16),
      };
      const showGraph = look.constellation && st !== 'offline';

      eclipse.halo(ef);
      if (showGraph) constellation.back(cf);
      eclipse.body(ef, () => {
        // the inner network, projected inside the void
        const cY = Math.cos(rotY), sY = Math.sin(rotY);
        const rotX = -0.2 + Math.sin(time * 0.23) * 0.08, cX = Math.cos(rotX), sX = Math.sin(rotX);
        const inner = R * 0.74, fov = 3.2;
        for (let i = 0; i < nCount; i++) {
          const x = net.x[i], y = net.y[i], z = net.z[i];
          const x2 = x * cY - z * sY, z2 = x * sY + z * cY;
          const y2 = y * cX - z2 * sX, z3 = y * sX + z2 * cX;
          const s = fov / (fov + z3);
          pnx[i] = cx + x2 * inner * s; pny[i] = cy - y2 * inner * s; pnd[i] = (z3 + 1.15) / 2.3;
        }
        ctx.lineWidth = 1;
        for (let b = 0; b < 2; b++) {
          ctx.beginPath();
          for (let e = 0; e < nE; e++) {
            const a = net.ea[e], bb = net.eb[e];
            if (bb >= nCount) continue;
            if (((pnd[a] + pnd[bb]) / 2 < 0.5 ? 1 : 0) !== b) continue;
            ctx.moveTo(pnx[a], pny[a]); ctx.lineTo(pnx[bb], pny[bb]);
          }
          ctx.strokeStyle = `hsla(${curHue + 18}, ${curSat}%, ${light ? 45 : 70}%, ${Math.min(1, (0.035 + b * 0.05) * (0.4 + inten) * look.glow)})`;
          ctx.stroke();
        }
        const dot = glow(curHue + 18, curSat, light, 90, 16);
        for (let i = 0; i < nCount; i++) {
          const near = 1 - pnd[i];
          const tw = 0.7 + 0.3 * Math.sin(time * 2.1 + i * 2.7);
          const r = (1.2 + near * 1.6 + inten) * 1.6;
          ctx.globalAlpha = Math.min(1, (0.07 + near * 0.3) * tw * (0.4 + inten));
          ctx.drawImage(dot, pnx[i] - r, pny[i] - r, r * 2, r * 2);
        }
        const hot = glow(curHue, curSat, light, 97, 16);
        for (let i = 0; i < MAXP; i++) {
          if (!pLive[i]) continue;
          pT[i] += pSpeed[i] * dt * motion;
          const a = net.ea[pEdge[i]], b = net.eb[pEdge[i]];
          if (pT[i] >= 1 || b >= nCount) { pLive[i] = 0; continue; }
          const t = pDir[i] === 1 ? pT[i] : 1 - pT[i];
          const r = 5 + amp * 4;
          ctx.globalAlpha = Math.sin(pT[i] * Math.PI);
          ctx.drawImage(hot, pnx[a] + (pnx[b] - pnx[a]) * t - r, pny[a] + (pny[b] - pny[a]) * t - r, r * 2, r * 2);
        }
        ctx.globalAlpha = 1;
      });
      eclipse.motes(ef);
      if (!reduced) eclipse.streaks(ef);
      if (showGraph) constellation.front(cf);

      // tool beams: core → capability node → out toward Live Activity (left)
      const hot = glow(curHue, curSat, light, 97, 32);
      for (let i = beams.length - 1; i >= 0; i--) {
        const bm = beams[i];
        bm.t += dt * 1.1;
        if (bm.t >= 1) { beams.splice(i, 1); continue; }
        const [nx, ny] = (showGraph && constellation.position(bm.node, P.nodes)) || [cx - R * 1.8, cy];
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
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, ${light ? 38 : 70}%, ${Math.min(1, a * look.glow)})`;
        ctx.lineWidth = width;
        ctx.beginPath(); ctx.ellipse(cx, cy, rad, rad * 0.97, 0, 0, Math.PI * 2); ctx.stroke();
        ctx.lineWidth = 1;
      };
      if (!reduced) {
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
        const rad = rg.kind === 'in' ? R * (1.7 - q * 0.65) : rg.kind === 'burst' ? R * (1.02 + q * 1.1) : R * (1.04 + q * 0.9);
        ringStroke(rad, a, rg.kind === 'burst' ? 2 : 1);
      }
      if (st === 'synthesizing') ringStroke(R * (1.32 - ((time * 0.5) % 1) * 0.18), 0.24);
      // approval: a slow, deliberate breath (~0.3 Hz), never a flash
      if (st === 'awaiting_approval') {
        const breath = reduced ? 0.5 : 0.5 + 0.5 * Math.sin(time * 1.9);
        ringStroke(R * (1.2 + breath * 0.04), 0.14 + breath * 0.2, 1.5);
      }
      ctx.globalCompositeOperation = 'source-over';
      live.frameMs = live.frameMs * 0.9 + (performance.now() - frameStart) * 0.1;
    }
    const loop = createCanvasLoop(box,render,() => {
      const P = p.current;
      if (P.paused || !w || !h) return null;
      return canvasInterval(P.state,BUSY.has(P.state) || live.micLevel > .02,P.reducedMotion,live.frameMs);
    });
    refresh = loop.refresh;
    syncRef.current = refresh;
    return () => { syncRef.current = null; loop.stop(); ro.disconnect(); };
  }, []);
  useEffect(() => { syncRef.current?.(); }, [props.paused,props.reducedMotion,props.state]);

  return (
    <div ref={boxRef} className="pointer-events-none absolute inset-0">
      <canvas ref={canvasRef} className="block h-full w-full" aria-hidden="true" />
    </div>
  );
}

// Props change at conversation pace; the animation reads live refs per frame.
export default memo(NeuralCore);
