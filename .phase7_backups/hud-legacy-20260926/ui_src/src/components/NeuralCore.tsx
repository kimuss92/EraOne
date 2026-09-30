import { memo, useEffect, useRef, type MutableRefObject } from 'react';
import type { ActivityState } from '../hooks/useEra';

export type SphereMode = 'active' | 'standby';
export type QualityPreset = 'low' | 'medium' | 'high';

interface Props {
  mode: SphereMode;
  /** Current Era activity — drives pulse rate/speed and brightness, computed
   *  INSIDE the rAF loop so App never re-renders to animate it. */
  activity: ActivityState;
  /** Live voice amplitude ref (0..1) — read per frame, never via React state. */
  amplitudeRef: MutableRefObject<number>;
  /** Per-band energies of Era's voice (low → high, 0..1) — read per frame.
   *  Drives the equalizer displacement of the shell. */
  bandsRef: MutableRefObject<Float32Array>;
  /** Emotion accent hue (deg) + saturation (0-100) — mirrors the CSS --ah/--as
   *  accent pair. Lerped INSIDE the rAF loop so a mood change glides over ~1s. */
  hue: number;
  sat: number;
  quality?: QualityPreset;
  /** Light theme: darker strokes, normal compositing. */
  light?: boolean;
  /** Backend link down: the sphere stays visible but dims to a dormant
   *  residual glow with almost no signal traffic. */
  offline?: boolean;
}

// NEURAL CORE — a procedural 3D sphere: a spherical point cloud (the shell),
// a sparse set of brighter neuron nodes, precomputed synapse edges between
// them, and signal pulses that travel along the edges. Era's activity sets
// how often pulses fire and how fast they travel; speaking amplitude adds
// bursts, so the core visibly "lights up" when Era thinks/executes/speaks.
//
// ANTI-FLICKER RULES:
//   * full clearRect every frame — no alpha trail-fade accumulation
//   * geometry is built ONCE per quality preset — no mid-animation rebuilds
//   * no per-frame z-sorting — dots are small and alpha-blended, painter's
//     order is invisible, so nothing ever "pops"
//   * 30fps cap with a tolerance on the gate (see FRAME_TOLERANCE) — an exact
//     comparison sat right on a 60Hz display's beat and made frames alternate
//     33ms / 50ms
//   * every smoothed value uses `approach()` (exponential decay over real dt),
//     never a fixed per-frame fraction — otherwise an uneven frame advances
//     brightness further than an even one and the core shimmers
//   * DPR capped at 1.5 — low, steady load

// Dense clouds + several synapses per neuron — the core must read as a bright,
// luminous neural sphere, not a sparse scattering of dots.
const QUALITY: Record<QualityPreset, { cortex: number; neurons: number; maxPulses: number }> = {
  low: { cortex: 700, neurons: 130, maxPulses: 18 },
  medium: { cortex: 1200, neurons: 210, maxPulses: 30 },
  high: { cortex: 1750, neurons: 290, maxPulses: 42 },
};

const FPS_CAP = 30;
const FRAME_INTERVAL = 1000 / FPS_CAP;
// rAF ticks on the display's cadence, not ours. On a 60Hz screen the elapsed
// time lands on 16.67 / 33.33ms — exactly ON the 30fps threshold, so tiny
// scheduling jitter made frames alternate between passing at 33ms and missing
// and passing at 50ms instead. Every dt-driven value (brightness, rotation,
// pulses) then advanced in irregular steps: THE FLICKER. A small tolerance
// absorbs that jitter and the cadence stays even.
const FRAME_TOLERANCE = 2;
const STANDBY_SCALE = 0.45;

// EQUALIZER — the shell is split into latitude rings, one per voice band (low
// at the bottom, high at the top), and each ring's radius swells with that
// band's live energy. That is what makes the sphere *follow* Era's voice
// instead of merely brightening with it.
const EQ_BANDS = 8;          // keep in sync with _EQ_BANDS in main.py
const EQ_DISPLACE = 0.22;    // max radial swell, as a fraction of R
const EQ_ATTACK = 26;        // per-second approach rate — snappy on consonants
const EQ_RELEASE = 7;        // slower fall-off, so rings settle rather than snap
const LINKS_PER_NEURON = 3;
// Fraction of neurons that fire amber "computation sparks" (reference image
// has a scattering of warm highlights among the cyan).
const SPARK_RATIO = 0.07;
const SPARK_HUE = 34;

interface Geometry {
  // outer shell cloud (unit space, spherical)
  cx: Float32Array; cy: Float32Array; cz: Float32Array;
  // neuron nodes
  nx: Float32Array; ny: Float32Array; nz: Float32Array;
  // edges as neuron index pairs
  ea: Int32Array; eb: Int32Array;
  // Equalizer band index per point, as a FRACTIONAL position in 0..EQ_BANDS-1
  // (derived from latitude). Precomputed here so the render loop only has to
  // lerp two band energies per point instead of recomputing the mapping.
  cband: Float32Array; nband: Float32Array;
}

/** Fractional band index for a point at latitude `y` (unit sphere, -1..1).
 *  South pole = band 0 (lows), north pole = the top band (highs). */
function bandIndexFor(y: number): number {
  return ((y + 1) / 2) * (EQ_BANDS - 1);
}

// Deterministic PRNG so the core looks identical every launch (no re-roll
// "glitch" between sessions or standby transitions).
function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Map a unit-sphere direction to a surface point on the core.
 *
 *  The core is a SPHERE: the direction is already the answer, so the only
 *  displacement applied is a hair of radial jitter. It matters that this stays
 *  tiny (±1.5%) — enough that the shell reads as a cloud of individual points
 *  rather than a rigid wireframe ball, but far too small to dent the circular
 *  silhouette, which is the whole shape. */
function spherePoint(sx: number, sy: number, sz: number, jitter: number): [number, number, number] {
  const r = 1 + jitter * 0.03;
  return [sx * r, sy * r, sz * r];
}

function buildGeometry(preset: QualityPreset): Geometry {
  const { cortex, neurons } = QUALITY[preset];
  const rnd = mulberry32(1337);

  const cx = new Float32Array(cortex), cy = new Float32Array(cortex), cz = new Float32Array(cortex);
  const cband = new Float32Array(cortex);
  for (let i = 0; i < cortex; i++) {
    // fibonacci sphere directions for even coverage
    const t = (i + 0.5) / cortex;
    const th = Math.acos(1 - 2 * t);
    const ph = (i + 0.5) * Math.PI * (3 - Math.sqrt(5));
    const [x, y, z] = spherePoint(Math.sin(th) * Math.cos(ph), Math.cos(th), Math.sin(th) * Math.sin(ph), rnd() - 0.5);
    cx[i] = x; cy[i] = y; cz[i] = z;
    cband[i] = bandIndexFor(Math.cos(th));
  }

  // Neurons: a second, sparser fibonacci pass (offset) so they interleave the
  // surface cloud instead of duplicating its points.
  const nx = new Float32Array(neurons), ny = new Float32Array(neurons), nz = new Float32Array(neurons);
  const nband = new Float32Array(neurons);
  for (let i = 0; i < neurons; i++) {
    const t = (i + 0.73) / neurons;
    const th = Math.acos(1 - 2 * t);
    const ph = (i + 0.73) * Math.PI * (3 - Math.sqrt(5)) + 1.7;
    // slightly inset (0.92) so neurons sit "inside" the outer shell
    const [x, y, z] = spherePoint(Math.sin(th) * Math.cos(ph), Math.cos(th), Math.sin(th) * Math.sin(ph), rnd() - 0.5);
    nx[i] = x * 0.92; ny[i] = y * 0.92; nz[i] = z * 0.92;
    nband[i] = bandIndexFor(Math.cos(th));
  }

  // Synapses: k nearest neighbours per neuron (built once, O(n²) is fine at
  // this size). Dedupe with a set.
  const seen = new Set<number>();
  const ea: number[] = [], eb: number[] = [];
  for (let i = 0; i < neurons; i++) {
    const d: { j: number; dd: number }[] = [];
    for (let j = 0; j < neurons; j++) {
      if (j === i) continue;
      const dx = nx[i] - nx[j], dy = ny[i] - ny[j], dz = nz[i] - nz[j];
      d.push({ j, dd: dx * dx + dy * dy + dz * dz });
    }
    d.sort((p, q) => p.dd - q.dd);
    for (let k = 0; k < LINKS_PER_NEURON; k++) {
      const j = d[k].j;
      const key = i < j ? i * 4096 + j : j * 4096 + i;
      if (!seen.has(key)) { seen.add(key); ea.push(i < j ? i : j); eb.push(i < j ? j : i); }
    }
  }

  return { cx, cy, cz, nx, ny, nz, cband, nband, ea: Int32Array.from(ea), eb: Int32Array.from(eb) };
}

function NeuralCore({ mode, activity, amplitudeRef, bandsRef, hue, sat, quality = 'medium', light = false, offline = false }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const modeRef = useRef<SphereMode>(mode);
  const activityRef = useRef<ActivityState>(activity);
  const qualityRef = useRef<QualityPreset>(quality);
  const hueRef = useRef(hue);
  const satRef = useRef(sat);
  const lightRef = useRef(light);
  const offlineRef = useRef(offline);
  const hiddenRef = useRef(false);

  modeRef.current = mode;
  activityRef.current = activity;
  qualityRef.current = quality;
  hueRef.current = hue;
  satRef.current = sat;
  lightRef.current = light;
  offlineRef.current = offline;

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return;

    // Geometry is fixed for the session at the INITIAL quality — swapping
    // point counts mid-animation is exactly the kind of visible pop the old
    // sphere suffered from. (Standby dims/shrinks instead of down-rezzing.)
    const preset = QUALITY[qualityRef.current];
    const geo = buildGeometry(qualityRef.current);
    const nCortex = geo.cx.length;
    const nNeurons = geo.nx.length;
    const nEdges = geo.ea.length;

    // projected scratch buffers
    const pnx = new Float32Array(nNeurons), pny = new Float32Array(nNeurons), pnd = new Float32Array(nNeurons);

    // Pulse pool (fixed size — no GC churn)
    const MAXP = preset.maxPulses;
    const pEdge = new Int32Array(MAXP);
    const pT = new Float32Array(MAXP);
    const pSpeed = new Float32Array(MAXP);
    const pDir = new Int8Array(MAXP);
    const pLive = new Uint8Array(MAXP);
    const rnd = mulberry32(42);

    // Voice ripple rings — outward when speaking (voice emission), inward
    // when listening (incoming waves). Tiny fixed pool, stroke-only, so the
    // cost is a handful of arcs per frame. Skipped under reduced motion.
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const MAXR = 6;
    const rT = new Float32Array(MAXR);
    const rDir = new Int8Array(MAXR);   // 1 = outward (speaking), -1 = inward (listening)
    const rLive = new Uint8Array(MAXR);

    let raf = 0;
    let time = 0;
    let width = 0, height = 0, dpr = 1;
    let lastFrameTime = performance.now();
    let smoothIntensity = 0.25;
    let curHue = hueRef.current;
    let curSat = satRef.current;
    let currentScale = modeRef.current === 'standby' ? STANDBY_SCALE : 1.0;

    // Locally smoothed copy of the voice spectrum. The WS frames arrive at
    // ~33Hz and we draw at 30 — without this the rings would step to whatever
    // packet happened to be latest at draw time.
    const eq = new Float32Array(EQ_BANDS);

    // Frame-rate-INDEPENDENT approach. `x += (t - x) * k` (the old form) moves
    // by a fixed FRACTION per frame, so it advances further on a long frame
    // than a short one — with jittery frame times that alone makes brightness
    // shimmer. Exponential decay over real elapsed time converges identically
    // regardless of how the frames fell.
    const approach = (cur: number, target: number, rate: number, dt: number) =>
      cur + (target - cur) * (1 - Math.exp(-rate * dt));

    const resize = () => {
      const rect = container.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      width = rect.width; height = rect.height;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    const handleVisibility = () => { hiddenRef.current = document.hidden; };
    document.addEventListener('visibilitychange', handleVisibility);

    const render = () => {
      raf = requestAnimationFrame(render);
      const now = performance.now();
      if (now - lastFrameTime < FRAME_INTERVAL - FRAME_TOLERANCE) return;
      const dt = Math.min(0.1, (now - lastFrameTime) / 1000);
      lastFrameTime = now;

      // Hidden tab: clear once and idle — zero draw cost in the background.
      if (hiddenRef.current) { ctx.clearRect(0, 0, width, height); return; }
      time += dt;

      const act = activityRef.current;
      const standby = modeRef.current === 'standby';
      const isLight = lightRef.current;
      const amp = amplitudeRef.current;

      const offline = offlineRef.current;

      // Intensity curve per state (brightness + pulse budget)
      let target: number;
      if (offline) target = 0.10 + Math.sin(time * 0.6) * 0.03; // dormant residual glow
      else if (standby) target = 0.22 + Math.sin(time * 1.1) * 0.05; // slow breathing
      else if (act === 'speaking') target = Math.min(1.15, 0.80 + amp * 0.9);
      else if (act === 'processing') target = 0.95 + Math.sin(time * 4.2) * 0.10;
      else if (act === 'thinking') target = 0.85 + Math.sin(time * 2.9) * 0.09;
      else if (act === 'listening') target = 0.72 + Math.sin(time * 1.8) * 0.07;
      else target = 0.60 + Math.sin(time * 0.8) * 0.06; // online / idle — still alive
      smoothIntensity = approach(smoothIntensity, target, 2.6, dt);

      // Equalizer: follow the live spectrum with a fast attack and a slower
      // release, so a syllable snaps the ring out and it eases back down.
      // Silenced entirely off-voice, so nothing ripples when Era isn't talking.
      const src = bandsRef.current;
      const voicing = act === 'speaking' && !standby && !offline;
      let eqPeak = 0;
      for (let b = 0; b < EQ_BANDS; b++) {
        const t = voicing ? (src[b] ?? 0) : 0;
        eq[b] = approach(eq[b], t, t > eq[b] ? EQ_ATTACK : EQ_RELEASE, dt);
        if (eq[b] > eqPeak) eqPeak = eq[b];
      }
      // Skip the whole displacement path when the sphere is at rest — the
      // common case, and it keeps the idle cost exactly where it was.
      const eqActive = eqPeak > 0.002;

      /** Energy at a fractional band position, lerped between neighbours so the
       *  rings blend into one continuous surface instead of eight hard steps. */
      const eqAt = (bf: number) => {
        const i = bf | 0;
        const f = bf - i;
        const a = eq[i] ?? 0;
        const b = eq[i + 1] ?? a;
        return a + (b - a) * f;
      };

      // Accent glide — shortest hue path
      const hueDelta = ((hueRef.current - curHue + 540) % 360) - 180;
      curHue += hueDelta * (1 - Math.exp(-1.4 * dt));
      curSat = approach(curSat, satRef.current, 1.4, dt);

      // Scale glide (standby ↔ active)
      currentScale = approach(currentScale, standby ? STANDBY_SCALE : 1.0, 1.55, dt);

      // Pulse firing rate per state (expected spawns/second)
      const rate = offline ? 0.3
        : standby ? 0.8
        : act === 'speaking' ? 6 + amp * 26
        : act === 'processing' ? 22
        : act === 'thinking' ? 14
        : act === 'listening' ? 5
        : 2.5;
      const speedMul = offline || standby ? 0.35 : act === 'processing' ? 2.1 : act === 'thinking' ? 1.5 : 1.0;

      // spawn pulses
      let want = rate * dt;
      for (let p = 0; p < MAXP && want > 0; p++) {
        if (pLive[p]) continue;
        if (rnd() < want) {
          pLive[p] = 1;
          pEdge[p] = Math.floor(rnd() * nEdges);
          pT[p] = 0;
          pSpeed[p] = (0.9 + rnd() * 1.4) * speedMul;
          pDir[p] = rnd() < 0.5 ? -1 : 1;
        }
        want -= 1;
      }

      // ── draw ────────────────────────────────────────────────────────
      ctx.clearRect(0, 0, width, height); // full clear: crisp, no trails

      const cxm = width / 2, cym = height / 2;
      // FIXED SIZE, explicitly requested: the sphere is the same size at every
      // window size — maximizing the app must not inflate it (it ballooned to
      // fill a 4K window and dwarfed the panels). rCap is the one size it ever
      // renders at; the min(...) term only SHRINKS it when the centre column
      // physically can't seat it (the canvas IS the column, so width / height
      // here already exclude the rails, title and command bar).
      //
      // TWO TIERS — the exact mirror of `.core-geom` in index.css, which the
      // HUD tags and orbit rings position off. Below an 880px column the sphere
      // steps down so the tags have a margin to sit in; above it, full size.
      // If either formula changes, change BOTH or the HUD will drift off the
      // sphere's edge.
      const compact = width < 880;
      const rCap = compact ? 170 : 250;
      const rFrac = compact ? 0.30 : 0.42;
      const R = Math.min(rCap, Math.min(width, height) * rFrac) * currentScale;
      const fov = 3.2; // unit-space perspective

      const rotY = time * (standby ? 0.08 : 0.32);
      const rotX = -0.18 + Math.sin(time * 0.23) * (standby ? 0.03 : 0.1);
      const cY = Math.cos(rotY), sY = Math.sin(rotY);
      const cX = Math.cos(rotX), sX = Math.sin(rotX);

      // `swell` displaces the point radially — this is the equalizer. It is a
      // pure scale on the unit-space position, so the point stays on its own
      // axis and the sphere breathes ring by ring rather than deforming.
      const project = (
        x: number, y: number, z: number,
        swell: number,
        out: (px: number, py: number, d: number) => void,
      ) => {
        if (swell !== 1) { x *= swell; y *= swell; z *= swell; }
        const x2 = x * cY - z * sY;
        const z2 = x * sY + z * cY;
        const y2 = y * cX - z2 * sX;
        const z3 = y * sX + z2 * cX;
        const s = fov / (fov + z3);
        out(cxm + x2 * R * s, cym - y2 * R * s, (z3 + 1.15) / 2.3); // d: 0 near .. 1 far
      };

      const lit = (l: number) => (isLight ? Math.max(22, l - 26) : l);
      const inten = smoothIntensity;

      // NEON COMPOSITING — in dark mode every layer is drawn ADDITIVELY, so
      // overlapping dots, links and pulses accumulate into real luminous
      // bloom (the "electro signal" look) instead of flat alpha dots. Light
      // mode stays source-over: 'lighter' on a pale background blows out to
      // white. This is the single change that makes the core read as neon.
      ctx.globalCompositeOperation = isLight ? 'source-over' : 'lighter';

      // 0) volumetric core glow — one radial gradient behind the whole sphere.
      //    Cheap (a single fill) and it's what gives the structure its
      //    "lit from inside" body rather than a hollow point cloud.
      const glowR = R * 1.55;
      const halo = ctx.createRadialGradient(cxm, cym, R * 0.06, cxm, cym, glowR);
      const glowA = (isLight ? 0.10 : 0.30) * (0.35 + inten);
      halo.addColorStop(0, `hsla(${curHue}, ${curSat}%, ${lit(62)}%, ${glowA})`);
      halo.addColorStop(0.45, `hsla(${curHue}, ${curSat}%, ${lit(55)}%, ${glowA * 0.35})`);
      halo.addColorStop(1, `hsla(${curHue}, ${curSat}%, ${lit(50)}%, 0)`);
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(cxm, cym, glowR, 0, Math.PI * 2);
      ctx.fill();

      // 1) project neurons (needed for edges + nodes) — they ride the same
      //    equalizer swell as the shell, so the synapse web stretches with the
      //    voice instead of staying rigid inside a moving surface.
      for (let i = 0; i < nNeurons; i++) {
        const swell = eqActive ? 1 + eqAt(geo.nband[i]) * EQ_DISPLACE : 1;
        project(geo.nx[i], geo.ny[i], geo.nz[i], swell, (px, py, d) => { pnx[i] = px; pny[i] = py; pnd[i] = d; });
      }

      // 2) synapse edges — depth-faded, brighter, with a live current running
      //    through them (each edge breathes on its own phase, so the web looks
      //    electrically alive rather than statically wired).
      ctx.lineWidth = 1;
      for (let e = 0; e < nEdges; e++) {
        const a = geo.ea[e], b = geo.eb[e];
        const d = (pnd[a] + pnd[b]) / 2;
        const current = 0.7 + 0.3 * Math.sin(time * 2.4 + e * 0.9);
        const alpha = (0.10 + (1 - d) * 0.30) * (0.5 + inten * 1.0) * current;
        ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, ${lit(66)}%, ${alpha})`;
        ctx.beginPath();
        ctx.moveTo(pnx[a], pny[a]);
        ctx.lineTo(pnx[b], pny[b]);
        ctx.stroke();
      }

      // 3) shell cloud — dense bright dots, depth-shaded (no sorting: small
      //    additive dots make painter's order imperceptible). Each dot rides
      //    its latitude band: it pushes OUT and lights UP with that band's
      //    energy, which is what reads as an equalizer wrapped on the sphere.
      for (let i = 0; i < nCortex; i++) {
        const e = eqActive ? eqAt(geo.cband[i]) : 0;
        const swell = eqActive ? 1 + e * EQ_DISPLACE : 1;
        project(geo.cx[i], geo.cy[i], geo.cz[i], swell, (px, py, d) => {
          const near = 1 - d;
          const alpha = 0.20 + near * 0.75 * (0.55 + inten * 0.8) + e * 0.35;
          const shimmer = Math.sin(i * 1.7 + time * 1.2) * 10;
          ctx.fillStyle = `hsla(${curHue + shimmer}, ${Math.min(100, curSat + 8)}%, ${lit(60 + near * 30 + e * 12)}%, ${alpha})`;
          const r = 0.7 + near * 1.2 + inten * 0.7 + e * 1.1;
          ctx.beginPath();
          ctx.arc(px, py, r, 0, Math.PI * 2);
          ctx.fill();
        });
      }

      // 4) neuron nodes — two-layer (soft halo + hot core) so each one reads
      //    as a glowing lamp. Every ~14th node is an amber computation spark
      //    that flares on its own rhythm, matching the warm highlights in the
      //    reference image; sparks flare hardest while Era is thinking.
      const sparkEvery = Math.max(1, Math.round(1 / SPARK_RATIO));
      const sparkBoost = act === 'thinking' || act === 'processing' ? 1.6 : 1.0;
      for (let i = 0; i < nNeurons; i++) {
        const near = 1 - pnd[i];
        const isSpark = !offline && i % sparkEvery === 0;
        const tw = 0.7 + 0.3 * Math.sin(time * 2.1 + i * 2.7);
        const flare = isSpark
          ? Math.max(0, Math.sin(time * 1.5 + i * 1.3)) ** 3 * sparkBoost
          : 0;
        const h = isSpark && flare > 0.15 ? SPARK_HUE : curHue;
        const s = isSpark && flare > 0.15 ? 95 : curSat;

        const e = eqActive ? eqAt(geo.nband[i]) : 0;
        const base = (0.30 + near * 0.65) * tw * (0.5 + inten * 0.9);
        const a2 = base + flare * 0.5 + e * 0.4;
        const r = 1.2 + near * 1.5 + inten * 1.0 + flare * 1.6 + e * 1.4;

        // soft halo
        ctx.fillStyle = `hsla(${h}, ${s}%, ${lit(60)}%, ${a2 * 0.22})`;
        ctx.beginPath(); ctx.arc(pnx[i], pny[i], r * 2.6, 0, Math.PI * 2); ctx.fill();
        // hot core
        ctx.fillStyle = `hsla(${h}, ${Math.min(100, s + 10)}%, ${lit(74 + near * 16)}%, ${a2})`;
        ctx.beginPath(); ctx.arc(pnx[i], pny[i], r, 0, Math.PI * 2); ctx.fill();
      }

      // 5) travelling pulses — the electro signals. Three layers (wide bloom,
      //    halo, white-hot core) plus a short comet tail along the edge.
      for (let p = 0; p < MAXP; p++) {
        if (!pLive[p]) continue;
        pT[p] += pSpeed[p] * dt;
        if (pT[p] >= 1) { pLive[p] = 0; continue; }
        const e = pEdge[p];
        const a = geo.ea[e], b = geo.eb[e];
        const t = pDir[p] === 1 ? pT[p] : 1 - pT[p];
        const px = pnx[a] + (pnx[b] - pnx[a]) * t;
        const py = pny[a] + (pny[b] - pny[a]) * t;
        const d = pnd[a] + (pnd[b] - pnd[a]) * t;
        const near = 1 - d;
        const fade = Math.sin(pT[p] * Math.PI); // ease in/out along the edge

        // comet tail — a short bright segment trailing the head
        const tt = Math.max(0, Math.min(1, t - pDir[p] * 0.16));
        const tx = pnx[a] + (pnx[b] - pnx[a]) * tt;
        const ty = pny[a] + (pny[b] - pny[a]) * tt;
        ctx.strokeStyle = `hsla(${curHue}, ${Math.min(100, curSat + 10)}%, ${lit(72)}%, ${0.45 * fade})`;
        ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(px, py); ctx.stroke();
        ctx.lineWidth = 1;

        const bloom = 6 + near * 4 + amp * 4;
        ctx.fillStyle = `hsla(${curHue}, ${curSat}%, ${lit(60)}%, ${0.10 * fade})`;
        ctx.beginPath(); ctx.arc(px, py, bloom, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = `hsla(${curHue}, ${curSat}%, ${lit(68)}%, ${0.28 * fade})`;
        ctx.beginPath(); ctx.arc(px, py, 3.4 + near * 2.2, 0, Math.PI * 2); ctx.fill();
        // white-hot core — the spark itself
        ctx.fillStyle = `hsla(${curHue}, ${Math.min(100, curSat + 20)}%, ${lit(92)}%, ${fade})`;
        ctx.beginPath(); ctx.arc(px, py, 1.5 + near * 1.1, 0, Math.PI * 2); ctx.fill();
      }

      // 6) voice ripple rings — outward while speaking, inward while listening
      if (!reducedMotion && !standby && !offline) {
        const ringRate = act === 'speaking' ? 1.1 + amp * 3.0
          : act === 'listening' ? 0.8
          : 0;
        if (ringRate > 0) {
          let wantR = ringRate * dt;
          for (let r = 0; r < MAXR && wantR > 0; r++) {
            if (rLive[r]) continue;
            if (rnd() < wantR) {
              rLive[r] = 1;
              rT[r] = 0;
              rDir[r] = act === 'speaking' ? 1 : -1;
            }
            wantR -= 1;
          }
        }
        for (let r = 0; r < MAXR; r++) {
          if (!rLive[r]) continue;
          rT[r] += dt * 0.55; // ~1.8s life
          if (rT[r] >= 1) { rLive[r] = 0; continue; }
          const p = rT[r];
          const radius = rDir[r] === 1
            ? R * (0.55 + p * 1.05)   // emitted from the core, expanding
            : R * (1.6 - p * 1.05);   // incoming wave, contracting
          const alpha = Math.sin(p * Math.PI) * 0.22 * (0.5 + inten);
          ctx.strokeStyle = `hsla(${curHue}, ${curSat}%, ${lit(64)}%, ${alpha})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.ellipse(cxm, cym, radius, radius * 0.94, 0, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
    };

    raf = requestAnimationFrame(render);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  return (
    <div ref={containerRef} className="pointer-events-none absolute inset-0 z-0">
      <canvas ref={canvasRef} className="pointer-events-none block h-full w-full" />
    </div>
  );
}

// Memoized: props are low-frequency strings + a stable ref, so App re-renders
// (logs, volume meter, metrics) never touch the canvas subtree.
export default memo(NeuralCore);
