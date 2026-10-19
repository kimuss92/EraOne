import type { AgentState } from '../../lib/types';
import type { Density } from '../../lib/appearance';
import { glow, ring, voidDisc, RING_R } from './sprites';

// THE ECLIPSE — Era's voice orb (after "Voice AI visual design", G. Kuznetsov):
// a black void rimmed by a luminous ring, fine fibres reaching inward from the
// rim, drifting motes and, while Era thinks, a tilting 3-D ring with light
// streaks pulled into it. Every live motion maps to a REAL signal:
//
//   fibres        speaking → the 8 band energies of Era's real voice audio
//                 listening → the real mic RMS; thinking → a rotating wave
//   ring tilt     routing / thinking / tool_running (and eases back after)
//   ring breath   Era's real output amplitude
//   streaks       firing rate follows the agent state's tuning
//
// Cost: ~1 halo + 1 void + 1–3 ring blits, 2 batched fibre strokes, ≤60 mote
// and ≤18 streak blits per frame. No per-frame gradients, no filters.

const FIBRES: Record<Density, number> = { low: 100, medium: 170, high: 240 };
const MOTES: Record<Density, number> = { low: 22, medium: 40, high: 60 };
const MAX_FIBRES = 240, MAX_MOTES = 60, MAX_STREAKS = 18;
const TILTED = new Set<AgentState>(['routing', 'thinking', 'tool_running']);
const STREAK_RATE: Partial<Record<AgentState, number>> = {
  thinking: 6, tool_running: 9, routing: 7, wake_detected: 10, synthesizing: 2, idle: 0.25,
};

export interface EclipseFrame {
  ctx: CanvasRenderingContext2D;
  cx: number; cy: number; R: number;
  hue: number; sat: number; light: boolean;
  inten: number; time: number; dt: number; motion: number;
  st: AgentState; amp: number; mic: number;
  eqAt: (band: number) => number; eqOn: boolean; eqBands: number;
  glowK: number; density: Density;
}

export function createEclipse(rnd: () => number) {
  // fibres: angle, rest length, personal variance (precomputed once)
  const fc = new Float32Array(MAX_FIBRES), fs = new Float32Array(MAX_FIBRES);
  const fBase = new Float32Array(MAX_FIBRES), fSeed = new Float32Array(MAX_FIBRES);
  const fAng = new Float32Array(MAX_FIBRES), fBand = new Float32Array(MAX_FIBRES);
  for (let i = 0; i < MAX_FIBRES; i++) {
    const a = (i / MAX_FIBRES) * Math.PI * 2 + (rnd() - 0.5) * 0.02;
    const seed = rnd();
    const below = Math.max(0, Math.sin(a)); // canvas y grows downward: sin>0 is the lower rim
    fAng[i] = a; fc[i] = Math.cos(a); fs[i] = Math.sin(a); fSeed[i] = seed;
    fBase[i] = 0.03 + 0.17 * below * below * (0.55 + 0.9 * seed) + 0.025 * seed;
    // 0 at the bottom of the rim, 1 at the top (mirror-symmetric)
    fBand[i] = Math.abs(((a - Math.PI / 2 + Math.PI * 3) % (Math.PI * 2)) - Math.PI) / Math.PI;
  }
  // stride through the fibre table so lower densities stay evenly spread
  const order = Int16Array.from({ length: MAX_FIBRES }, (_, i) => (i * 97) % MAX_FIBRES);

  const mA = new Float32Array(MAX_MOTES), mR = new Float32Array(MAX_MOTES), mV = new Float32Array(MAX_MOTES);
  const mPh = new Float32Array(MAX_MOTES), mS = new Float32Array(MAX_MOTES);
  for (let i = 0; i < MAX_MOTES; i++) {
    mA[i] = rnd() * Math.PI * 2; mR[i] = 1.04 + rnd() * 0.55; mV[i] = (0.04 + rnd() * 0.16) * (rnd() < 0.5 ? -1 : 1);
    mPh[i] = rnd() * 10; mS[i] = 2 + rnd() * 3.5;
  }

  const sx = new Float32Array(MAX_STREAKS), sy = new Float32Array(MAX_STREAKS);
  const svx = new Float32Array(MAX_STREAKS), svy = new Float32Array(MAX_STREAKS);
  const sLife = new Float32Array(MAX_STREAKS), sT = new Float32Array(MAX_STREAKS);
  const sLive = new Uint8Array(MAX_STREAKS);

  let tilt = 0, sec0 = 0, sec1 = 0, streakAcc = 0;
  const approach = (c: number, t: number, r: number, dt: number) => c + (t - c) * (1 - Math.exp(-r * dt));

  function ringSize(f: EclipseFrame): number {
    return (f.R / RING_R) * (1 + f.amp * 0.05 + f.mic * 0.02);
  }

  /** Behind everything: the wide atmospheric halo. */
  function halo(f: EclipseFrame): void {
    const { ctx, cx, cy, R } = f;
    ctx.globalAlpha = Math.min(1, (f.light ? 0.12 : 0.34) * (0.35 + f.inten) * f.glowK);
    ctx.drawImage(glow(f.hue, f.sat, f.light, 62, 128), cx - R * 2.3, cy - R * 2.3, R * 4.6, R * 4.6);
    ctx.globalAlpha = 1;
  }

  /** The void, fibres and ring share one transform so the whole eclipse tilts. */
  function body(f: EclipseFrame, drawInterior: () => void): void {
    const { ctx, cx, cy, R, dt, time } = f;
    tilt = approach(tilt, TILTED.has(f.st) ? 1 : 0, TILTED.has(f.st) ? 2.2 : 1.2, dt);
    const tiltY = 1 - 0.36 * tilt * (0.75 + 0.25 * Math.sin(time * 0.9));
    const orient = tilt * (0.5 + 0.32 * Math.sin(time * 0.37));

    // 1. the void occludes the spokes behind it (source-over even in dark mode)
    ctx.save();
    ctx.translate(cx, cy); ctx.rotate(orient); ctx.scale(1, tiltY);
    ctx.globalCompositeOperation = 'source-over';
    ctx.drawImage(voidDisc(f.hue, f.light), -R * 1.01, -R * 1.01, R * 2.02, R * 2.02);
    ctx.restore();
    ctx.globalCompositeOperation = f.light ? 'source-over' : 'lighter';

    // 2. Era's inner network lives inside the void (drawn by the caller)
    drawInterior();

    ctx.save();
    ctx.translate(cx, cy); ctx.rotate(orient); ctx.scale(1, tiltY);
    fibres(f);
    const D = ringSize(f);
    ctx.globalAlpha = Math.min(1, (0.6 + 0.45 * f.inten) * Math.min(1.25, f.glowK));
    ctx.drawImage(ring(f.hue, f.sat, f.light), -D / 2, -D / 2, D, D);
    if (f.motion > 0 && f.st !== 'offline' && f.st !== 'standby') {
      // One short arc in the existing canvas: no conic gradient or extra layer.
      const phase = time * .45;
      ctx.beginPath(); ctx.arc(0,0,R*1.025,phase,phase+.34);
      ctx.strokeStyle = `hsla(${f.hue+8},${f.sat}%,85%,.42)`;
      ctx.lineWidth = 1.4; ctx.stroke();
    }
    // a comet of light running the rim while Era works
    if (tilt > 0.05 || f.st === 'transcribing') {
      const a = time * 1.9, hot = glow(f.hue, f.sat, f.light, 97, 32);
      ctx.globalAlpha = Math.min(1, 0.4 + tilt * 0.6);
      ctx.drawImage(hot, Math.cos(a) * R - 11, Math.sin(a) * R - 11, 22, 22);
      ctx.drawImage(hot, Math.cos(a + Math.PI) * R - 7, Math.sin(a + Math.PI) * R - 7, 14, 14);
    }
    ctx.restore();
    ctx.globalAlpha = 1;
    secondaryRings(f);
  }

  function fibres(f: EclipseFrame): void {
    const { ctx, R, time, st, inten } = f;
    const n = FIBRES[f.density];
    // Voice drives the fibres whenever Era's audio is actually playing (the
    // state label can lag the first syllable by a beat).
    const speaking = f.eqOn || f.amp > 0.004;
    const loud = Math.min(1, f.amp * 1.6);
    const hearing = st === 'listening' || st === 'transcribing' || st === 'idle' || st === 'wake_detected';
    const working = TILTED.has(st) || st === 'synthesizing';
    const outer = R * 0.985;
    ctx.lineWidth = 1;
    for (let pass = 0; pass < 2; pass++) {
      ctx.beginPath();
      for (let k = 0; k < n; k++) {
        const i = order[k];
        let len = fBase[i] * (0.75 + inten * 0.5);
        // Mirrored spectrum: bass at the bottom of the rim (the longest
        // fibres), highs toward the top, the same on both sides — so each
        // syllable reads as one shape instead of scattered noise.
        if (speaking) len += f.eqAt(fBand[i] * (f.eqBands - 1)) * (0.3 + 0.16 * fSeed[i]) + loud * (0.05 + 0.07 * fSeed[i]);
        else if (hearing) len += f.mic * (0.08 + 0.2 * fSeed[i]);
        if (working) { const w = Math.max(0, Math.sin(fAng[i] * 4 + time * 3.2)); len += 0.075 * w * w; }
        if ((len > 0.13 ? 1 : 0) !== pass) continue;
        const inner = R * (0.985 - Math.min(0.6, len));
        ctx.moveTo(fc[i] * outer, fs[i] * outer);
        ctx.lineTo(fc[i] * inner, fs[i] * inner);
      }
      const a = (pass ? 0.34 : 0.15) * (0.5 + inten) * f.glowK * (1 + loud * 0.9);
      ctx.strokeStyle = `hsla(${f.hue + 8}, ${f.sat}%, ${f.light ? 30 : 74}%, ${Math.min(0.9, a)})`;
      ctx.stroke();
    }
    // sparkles at a few fibre tips
    const dot = glow(f.hue, f.sat, f.light, 95, 16);
    for (let k = 0; k < n; k += 7) {
      const i = order[k];
      const tw = Math.max(0, Math.sin(time * 2.6 * f.motion + fSeed[i] * 20));
      if (tw < 0.4) continue;
      const rr = R * (0.985 - fBase[i] * 0.9);
      ctx.globalAlpha = tw * 0.7 * (0.4 + inten);
      ctx.drawImage(dot, fc[i] * rr - 2.5, fs[i] * rr - 2.5, 5, 5);
    }
    ctx.globalAlpha = 1;
  }

  function secondaryRings(f: EclipseFrame): void {
    const { ctx, cx, cy, time, st, dt } = f;
    const busy = TILTED.has(st);
    sec0 = approach(sec0, busy ? 0.55 : st === 'idle' || st === 'speaking' ? 0.16 : st === 'offline' ? 0 : 0.1, 1.5, dt);
    sec1 = approach(sec1, busy ? 0.4 : st === 'transcribing' || st === 'synthesizing' ? 0.22 : 0, 1.5, dt);
    const D = ringSize(f), tex = ring(f.hue + 22, f.sat, f.light);
    const draw = (a: number, scale: number, rot: number, squash: number) => {
      if (a < 0.01) return;
      ctx.save();
      ctx.translate(cx, cy); ctx.rotate(rot); ctx.scale(1, squash);
      ctx.globalAlpha = Math.min(1, a * f.glowK);
      ctx.drawImage(tex, -D * scale / 2, -D * scale / 2, D * scale, D * scale);
      ctx.restore();
    };
    draw(sec0, 1.14, time * 0.33 + 0.4, 0.3 + 0.06 * Math.sin(time * 0.5));
    draw(sec1, 1.26, -time * 0.45 + 2.1, 0.42);
    ctx.globalAlpha = 1;
  }

  /** Motes drifting around the rim; Era's voice pushes them outward. */
  function motes(f: EclipseFrame): void {
    const { ctx, cx, cy, R, dt, time } = f;
    const n = MOTES[f.density], dot = glow(f.hue + 10, f.sat, f.light, 94, 16);
    const push = 1 + f.amp * 0.3;
    for (let i = 0; i < n; i++) {
      mA[i] += mV[i] * dt * f.motion * (0.6 + f.inten);
      const r = R * mR[i] * push;
      const tw = Math.max(0, Math.sin(time * 1.7 + mPh[i]));
      ctx.globalAlpha = Math.min(1, (0.12 + 0.5 * tw * tw) * (0.35 + f.inten) * f.glowK);
      const s = mS[i];
      ctx.drawImage(dot, cx + Math.cos(mA[i]) * r - s / 2, cy + Math.sin(mA[i]) * r * 0.92 - s / 2, s, s);
    }
    ctx.globalAlpha = 1;
  }

  /** Light streaks pulled toward the ring while Era is working. */
  function streaks(f: EclipseFrame): void {
    const { ctx, cx, cy, R, dt, st } = f;
    streakAcc += (STREAK_RATE[st] ?? 0) * dt * f.motion;
    for (let i = 0; i < MAX_STREAKS && streakAcc >= 1; i++) {
      if (sLive[i]) continue;
      streakAcc -= 1;
      const a = -Math.PI * 0.25 + (rnd() - 0.5) * Math.PI * 1.4; // biased to the upper right
      const d = R * (2 + rnd() * 1.1);
      sx[i] = cx + Math.cos(a) * d; sy[i] = cy + Math.sin(a) * d;
      const speed = R * (1.5 + rnd() * 1.4);
      svx[i] = (-Math.cos(a) * 0.86 - Math.sin(a) * 0.5) * speed;
      svy[i] = (-Math.sin(a) * 0.86 + Math.cos(a) * 0.5) * speed;
      sT[i] = 0; sLife[i] = 0.6 + rnd() * 0.5; sLive[i] = 1;
    }
    if (streakAcc > 1) streakAcc = 1;
    const hot = glow(f.hue, f.sat, f.light, 97, 16);
    ctx.lineWidth = 1.2;
    for (let pass = 0; pass < 2; pass++) {
      ctx.beginPath();
      let any = false;
      for (let i = 0; i < MAX_STREAKS; i++) {
        if (!sLive[i]) continue;
        if (pass === 0) {
          sT[i] += dt * f.motion;
          sx[i] += svx[i] * dt * f.motion; sy[i] += svy[i] * dt * f.motion;
          const dx = sx[i] - cx, dy = sy[i] - cy;
          if (sT[i] >= sLife[i] || dx * dx + dy * dy < R * R * 1.1) { sLive[i] = 0; continue; }
        }
        const q = sT[i] / sLife[i];
        if ((q < 0.5 ? 0 : 1) !== pass) continue;
        any = true;
        ctx.moveTo(sx[i], sy[i]);
        ctx.lineTo(sx[i] - svx[i] * 0.11, sy[i] - svy[i] * 0.11);
      }
      if (!any) continue;
      ctx.strokeStyle = `hsla(${f.hue + 12}, ${f.sat}%, ${f.light ? 35 : 80}%, ${Math.min(1, (pass ? 0.22 : 0.42) * f.glowK)})`;
      ctx.stroke();
    }
    for (let i = 0; i < MAX_STREAKS; i++) {
      if (!sLive[i]) continue;
      ctx.globalAlpha = Math.sin((sT[i] / sLife[i]) * Math.PI) * 0.8;
      ctx.drawImage(hot, sx[i] - 3, sy[i] - 3, 6, 6);
    }
    ctx.globalAlpha = 1;
    ctx.lineWidth = 1;
  }

  function clear(): void { sLive.fill(0); streakAcc = 0; }

  return { halo, body, motes, streaks, clear };
}
