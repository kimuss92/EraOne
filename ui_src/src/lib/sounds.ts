import { useSyncExternalStore } from 'react';
import { getState, live, subscribe } from './store';

// HUD sound effects — synthesized with WebAudio (no files, no dependency).
// Each cue has its own shape so it is recognisable without looking:
//   wake      rising three-note chime        ready     warm resolved chord
//   toolStart soft low blip                  tool      two bright pings
//   error     descending low pair            focus     filtered air sweep (view change)
//   notify    two gentle pings               approval  three attention pulses
//   hover     a faint tick                   click     a short tap
// Gentle by design (master ~-14 dB, soft attack). Cues stay silent while the
// mic is capturing the user (listening / transcribing), so they never reach
// speech recognition; the wake chime is the one exception (it IS the reply).
// Only hover/click play while Era speaks: they answer the user's own hand.

export type Cue = 'wake' | 'toolStart' | 'tool' | 'error' | 'ready' | 'focus' | 'notify' | 'approval' | 'hover' | 'click';
const KEY = 'era_hud_sounds';
let enabled = false;
try { enabled = localStorage.getItem(KEY) === '1'; } catch { /* private mode */ }
let ctx: AudioContext | null = null;
let out: GainNode | null = null;
let noise: AudioBuffer | null = null;
const lastCue = new Map<Cue, number>();
const voices = new Set<AudioScheduledSourceNode>();
const listeners = new Set<() => void>();
export function useSoundsEnabled(): boolean {
  return useSyncExternalStore(cb => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => enabled);
}
export function setSoundsEnabled(on: boolean, persist = true): void {
  try {
    if (!persist && localStorage.getItem(KEY) !== null) return;
    if (persist) localStorage.setItem(KEY, on ? '1' : '0');
  } catch { /* private mode */ }
  enabled = on;
  if (!on) silence();
  listeners.forEach(cb => cb());
  if (on && persist) window.setTimeout(() => cue('ready'), 60);
}
function silence() {
  voices.forEach(v => { try { v.stop(); } catch { /* already ended */ } });
  voices.clear();
}
const CAPTURE = new Set(['listening', 'transcribing']);
const capturing = () => CAPTURE.has(getState().agent.state) || live.micLevel > 0.02;
const speaking = () => live.amplitude > 0.001 || ['speaking', 'synthesizing'].includes(getState().agent.state);
subscribe(() => { if (capturing()) silence(); });
document.addEventListener('visibilitychange', () => { if (document.hidden) silence(); });

// Audio is unlocked only inside a real user gesture; events never resume it.
function unlock(e: Event) {
  if (!e.isTrusted || !enabled) return;
  try {
    if (!ctx) {
      ctx = new AudioContext();
      out = ctx.createGain(); out.gain.value = 0.2;
      const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 7000;
      out.connect(lp).connect(ctx.destination);
      noise = ctx.createBuffer(1, ctx.sampleRate * 0.5, ctx.sampleRate);
      const d = noise.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    void ctx.resume().catch(() => {});
  } catch { /* no audio */ }
}
document.addEventListener('pointerdown', unlock, true);
document.addEventListener('keydown', unlock, true);

function track(src: AudioScheduledSourceNode, ...nodes: AudioNode[]) {
  voices.add(src);
  src.onended = () => { voices.delete(src); src.disconnect(); nodes.forEach(n => n.disconnect()); };
}
/** One enveloped oscillator; `to` glides the pitch over the note. */
function tone(freq: number, at: number, dur: number, gain: number, type: OscillatorType = 'sine', to?: number) {
  if (!ctx || !out) return;
  const osc = ctx.createOscillator(), g = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, at);
  if (to) osc.frequency.exponentialRampToValueAtTime(to, at + dur);
  g.gain.setValueAtTime(0, at);
  g.gain.linearRampToValueAtTime(gain, at + Math.min(0.015, dur / 4));
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  osc.connect(g).connect(out);
  track(osc, g);
  osc.start(at); osc.stop(at + dur + 0.02);
}
/** Band-passed noise sweeping from `f0` to `f1` — an "air" whoosh. */
function air(at: number, dur: number, gain: number, f0: number, f1: number) {
  if (!ctx || !out || !noise) return;
  const src = ctx.createBufferSource(), bp = ctx.createBiquadFilter(), g = ctx.createGain();
  src.buffer = noise; bp.type = 'bandpass'; bp.Q.value = 1.4;
  bp.frequency.setValueAtTime(f0, at); bp.frequency.exponentialRampToValueAtTime(f1, at + dur);
  g.gain.setValueAtTime(0, at); g.gain.linearRampToValueAtTime(gain, at + dur * 0.35); g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  src.connect(bp).connect(g).connect(out);
  track(src, bp, g);
  src.start(at); src.stop(at + dur + 0.02);
}

const GAP: Partial<Record<Cue, number>> = { hover: 70, click: 60 };
export function cue(kind: Cue): void {
  if (!enabled || !ctx || ctx.state !== 'running' || document.hidden) return;
  const ui = kind === 'hover' || kind === 'click';
  if (kind !== 'wake' && capturing()) return;
  if (!ui && kind !== 'wake' && speaking()) return;
  const now = performance.now();
  if (now - (lastCue.get(kind) ?? -Infinity) < (GAP[kind] ?? 250)) return;
  lastCue.set(kind, now);
  const t = ctx.currentTime + 0.005;
  try {
    switch (kind) {
      case 'wake': tone(523, t, 0.18, 0.5); tone(784, t + 0.07, 0.2, 0.45); tone(1047, t + 0.14, 0.32, 0.4); break;
      case 'ready': tone(392, t, 0.5, 0.3, 'triangle'); tone(523, t + 0.05, 0.5, 0.28); tone(659, t + 0.1, 0.55, 0.24); break;
      case 'toolStart': tone(330, t, 0.09, 0.35, 'triangle', 440); break;
      case 'tool': tone(1175, t, 0.12, 0.3); tone(1568, t + 0.08, 0.22, 0.28); break;
      case 'error': tone(330, t, 0.16, 0.5, 'triangle'); tone(247, t + 0.13, 0.3, 0.5, 'triangle'); break;
      case 'focus': air(t, 0.32, 0.5, 500, 2600); tone(294, t, 0.22, 0.2, 'sine', 440); break;
      case 'notify': tone(988, t, 0.14, 0.3); tone(1319, t + 0.1, 0.2, 0.26); break;
      case 'approval': [0, 0.16, 0.32].forEach(d => tone(880, t + d, 0.11, 0.38, 'triangle')); break;
      case 'hover': tone(2093, t, 0.03, 0.06); break;
      case 'click': tone(660, t, 0.05, 0.22, 'triangle', 520); break;
    }
  } catch { /* decorative audio never interrupts the HUD */ }
}

// Pointer feedback on every control: a faint tick entering a button, a tap on
// press. The view tabs already play the "focus" sweep, so they skip the tap.
const CONTROL = 'button:not(:disabled), [role="button"], [role="switch"], [role="slider"], a[href]';
document.addEventListener('pointerover', e => {
  const el = (e.target as Element | null)?.closest?.(CONTROL);
  if (el && !el.contains(e.relatedTarget as Node | null)) cue('hover');
}, { passive: true });
document.addEventListener('click', e => {
  const el = (e.target as Element | null)?.closest?.(CONTROL);
  if (el && !el.closest('.era-view-nav')) cue('click');
}, { passive: true });
