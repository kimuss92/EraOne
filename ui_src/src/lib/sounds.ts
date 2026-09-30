// Subtle HUD cues, synthesized with Web Audio (no audio files). Off unless the
// user enables "HUD sounds" in Settings. Era's own voice is played by the
// backend on its configured output device and is never routed through here.

type Cue = 'wake' | 'tool' | 'error' | 'ready';

let enabled = false;
let ctx: AudioContext | null = null;

export function setSoundsEnabled(on: boolean): void {
  enabled = on;
}

function tone(freq: number, start: number, dur: number, gain: number, type: OscillatorType = 'sine') {
  if (!ctx) return;
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  g.gain.setValueAtTime(0, start);
  g.gain.linearRampToValueAtTime(gain, start + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
  osc.connect(g).connect(ctx.destination);
  osc.start(start);
  osc.stop(start + dur + 0.02);
}

export function cue(kind: Cue): void {
  if (!enabled) return;
  try {
    ctx = ctx || new AudioContext();
    if (ctx.state === 'suspended') void ctx.resume();
    const t = ctx.currentTime;
    if (kind === 'wake') {
      tone(660, t, 0.12, 0.05);
      tone(990, t + 0.09, 0.16, 0.05);
    } else if (kind === 'tool') {
      tone(1320, t, 0.06, 0.03, 'triangle');
    } else if (kind === 'ready') {
      tone(880, t, 0.18, 0.025);
    } else {
      tone(220, t, 0.22, 0.05, 'triangle');
    }
  } catch {
    /* audio unavailable: cues are decoration, never required */
  }
}
