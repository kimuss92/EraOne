// Pre-rendered bitmaps for the core canvas. Everything expensive (radial
// gradients, blurred halos, text) is drawn ONCE into an offscreen canvas and
// then only blitted with drawImage per frame — the technique that keeps the
// eclipse core cheaper than the old 1100-dot sphere.
//
// Hues are quantised into buckets, and at most ONE new expensive sprite is
// built per frame (`beginFrame`): during a state colour glide the nearest
// cached bitmap is reused for a frame instead of stalling on a rebuild.

const cache = new Map<string, HTMLCanvasElement>();
let builtThisFrame = false;
const lastByKind = new Map<string, HTMLCanvasElement>();

export function beginFrame(): void { builtThisFrame = false; }

function canvas(w: number, h = w): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  return [c, c.getContext('2d')!];
}

function remember(kind: string, key: string, c: HTMLCanvasElement): HTMLCanvasElement {
  if (cache.size > 220) cache.clear();
  cache.set(key, c);
  lastByKind.set(kind, c);
  return c;
}

/** Soft round glow (dots, nodes, pulses). Cheap to build, never throttled. */
export function glow(h: number, s: number, light: boolean, hot = 92, size = 32): HTMLCanvasElement {
  const key = `g|${Math.round(h / 3) * 3}|${Math.round(s / 5) * 5}|${light ? 1 : 0}|${hot}|${size}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const [c, g] = canvas(size);
  const m = size / 2;
  const grad = g.createRadialGradient(m, m, 0, m, m, m);
  const l = light ? 32 : 62;
  grad.addColorStop(0, `hsla(${h}, ${Math.min(100, s + 10)}%, ${light ? 28 : hot}%, 1)`);
  grad.addColorStop(0.28, `hsla(${h}, ${s}%, ${l}%, 0.85)`);
  grad.addColorStop(0.55, `hsla(${h}, ${s}%, ${l}%, 0.22)`);
  grad.addColorStop(1, `hsla(${h}, ${s}%, ${l}%, 0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return remember('glow', key, c);
}

export const RING_SIZE = 512;
/** Ring radius inside the ring sprite, as a fraction of its size. */
export const RING_R = 0.36;

/** The eclipse ring: a luminous band, brighter on top, violet-dim below. */
export function ring(h: number, s: number, light: boolean): HTMLCanvasElement {
  const hb = Math.round(h / 8) * 8, sb = Math.round(s / 10) * 10;
  const key = `r|${hb}|${sb}|${light ? 1 : 0}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const prev = lastByKind.get('ring');
  if (builtThisFrame && prev) return prev;
  builtThisFrame = true;
  const [c, g] = canvas(RING_SIZE);
  const m = RING_SIZE / 2, r = RING_SIZE * RING_R;
  g.globalCompositeOperation = light ? 'source-over' : 'lighter';
  const band = (lum: number, a: number) => {
    const lg = g.createLinearGradient(0, m - r, 0, m + r);
    lg.addColorStop(0, `hsla(${hb}, ${sb}%, ${lum}%, ${a})`);
    lg.addColorStop(0.55, `hsla(${hb + 14}, ${sb}%, ${lum - 6}%, ${a * 0.55})`);
    lg.addColorStop(1, `hsla(${hb + 40}, ${Math.max(30, sb - 25)}%, ${lum - 12}%, ${a * 0.32})`);
    return lg;
  };
  const stroke = (width: number, blur: number, style: CanvasGradient) => {
    g.save();
    if (blur) g.filter = `blur(${blur}px)`;
    g.lineWidth = width;
    g.strokeStyle = style;
    g.beginPath(); g.arc(m, m, r, 0, Math.PI * 2); g.stroke();
    g.restore();
  };
  stroke(46, 26, band(light ? 40 : 52, 0.35));
  stroke(18, 9, band(light ? 38 : 60, 0.6));
  stroke(7, 3, band(light ? 34 : 72, 0.85));
  stroke(2.2, 0, band(light ? 22 : 93, 1));
  return remember('ring', key, c);
}

/** The eclipse void: a near-black disc with light bleeding in from below. */
export function voidDisc(h: number, light: boolean): HTMLCanvasElement {
  const hb = Math.round(h / 12) * 12;
  const key = `v|${hb}|${light ? 1 : 0}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const prev = lastByKind.get('void');
  if (builtThisFrame && prev) return prev;
  builtThisFrame = true;
  const size = 256, m = size / 2;
  const [c, g] = canvas(size);
  const base = g.createRadialGradient(m, m * 0.85, 0, m, m, m);
  base.addColorStop(0, light ? '#050a1c' : '#000105');
  base.addColorStop(0.75, light ? '#08122e' : '#01030c');
  base.addColorStop(0.97, `hsl(${hb}, 70%, ${light ? 16 : 7}%)`);
  base.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = base;
  g.beginPath(); g.arc(m, m, m, 0, Math.PI * 2); g.fill();
  // inner bleed: the ring's light scattering into the lower half of the void
  const bleed = g.createRadialGradient(m, m * 1.7, 0, m, m * 1.7, m * 1.05);
  bleed.addColorStop(0, `hsla(${hb + 20}, 80%, 45%, 0.28)`);
  bleed.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = bleed;
  g.beginPath(); g.arc(m, m, m * 0.97, 0, Math.PI * 2); g.fill();
  return remember('void', key, c);
}

const measure = document.createElement('canvas').getContext('2d')!;

/** Crisp text bitmap (labels): fillText is the slowest canvas primitive. */
export function label(text: string, color: string, dpr: number, font = '500 9px "JetBrains Mono Variable", monospace'): HTMLCanvasElement {
  const key = `t|${text}|${color}|${dpr}|${font}`;
  const hit = cache.get(key);
  if (hit) return hit;
  measure.font = font;
  const w = Math.ceil(measure.measureText(text).width) + 2;
  const [c, g] = canvas(Math.ceil(w * dpr), Math.ceil(14 * dpr));
  g.scale(dpr, dpr);
  g.font = font;
  g.textBaseline = 'middle';
  g.fillStyle = color;
  g.fillText(text, 1, 7);
  return remember('label', key, c);
}
