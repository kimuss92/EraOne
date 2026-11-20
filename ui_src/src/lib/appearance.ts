import { useSyncExternalStore } from 'react';

// Viewer-side look & feel (per browser, localStorage). Pure presentation: it
// never changes what Era does or which data is shown. The canvas reads `look`
// directly every frame, CSS reads the custom properties set on <html>, so a
// slider move costs one style recalc and zero React re-renders of the core.

export type Density = 'low' | 'medium' | 'high';

// Semantic state colours (amber approval, red error, green complete, steel
// standby) are never shifted: only Era's working-state blues follow a preset.
export const PRESETS = {
  eclipse: { label: 'Eclipse', accent: 214, sat: 92, shift: 0 },
  aurora: { label: 'Aurora', accent: 262, sat: 70, shift: 38 },
  classic: { label: 'Teal', accent: 174, sat: 40, shift: -28 },
  ember: { label: 'Rose', accent: 336, sat: 64, shift: 110 },
} as const;

export interface Look {
  /** identity colour preset (chrome + core tint for WORKING states only) */
  preset: keyof typeof PRESETS;
  /** whole-HUD zoom, 0.8 – 1.25 */
  uiScale: number;
  /** sphere size multiplier, 0.7 – 1.4 */
  coreScale: number;
  /** bloom / glow strength, 0.4 – 1.6 */
  glow: number;
  /** animation speed, 0.4 – 1.6 */
  speed: number;
  /** fibres, particles and synapse count */
  density: Density;
  /** panel surface opacity, 0.35 – 1 */
  panelOpacity: number;
  /** the tool constellation around the sphere */
  constellation: boolean;
  /** constellation labels + tool counts */
  labels: boolean;
}

export const DEFAULT_LOOK: Look = {
  preset: 'eclipse',
  uiScale: 1,
  coreScale: 1,
  glow: 1,
  speed: 1,
  density: 'medium',
  panelOpacity: 0.78,
  constellation: true,
  labels: true,
};

const KEY = 'era_hud_look_v1';
const clamp = (v: unknown, lo: number, hi: number, d: number) =>
  typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;

function sanitize(raw: Partial<Look> | null): Look {
  const r: Partial<Look> = raw && typeof raw === 'object' ? raw : {};
  return {
    preset: typeof r.preset === 'string' && r.preset in PRESETS ? r.preset : DEFAULT_LOOK.preset,
    uiScale: clamp(r.uiScale, 0.8, 1.25, DEFAULT_LOOK.uiScale),
    coreScale: clamp(r.coreScale, 0.7, 1.4, DEFAULT_LOOK.coreScale),
    glow: clamp(r.glow, 0.4, 1.6, DEFAULT_LOOK.glow),
    speed: clamp(r.speed, 0.4, 1.6, DEFAULT_LOOK.speed),
    density: r.density === 'low' || r.density === 'high' ? r.density : 'medium',
    panelOpacity: clamp(r.panelOpacity, 0.35, 1, DEFAULT_LOOK.panelOpacity),
    constellation: typeof r.constellation === 'boolean' ? r.constellation : true,
    labels: typeof r.labels === 'boolean' ? r.labels : true,
  };
}

function load(): Look {
  try { return sanitize(JSON.parse(localStorage.getItem(KEY) || 'null')); } catch { return { ...DEFAULT_LOOK }; }
}

/** Read per frame by the canvas — never replace the object, mutate it. */
export const look: Look = load();
const listeners = new Set<() => void>();
let version = 0;

function apply(): void {
  const p = PRESETS[look.preset];
  const root = document.documentElement.style;
  root.setProperty('--ah', String(p.accent));
  root.setProperty('--as', `${p.sat}%`);
  root.setProperty('--ui-scale', String(look.uiScale));
  root.setProperty('--panel-alpha', String(look.panelOpacity));
  root.setProperty('--glow', String(look.glow));
}
apply();

export function setLook(patch: Partial<Look>): void {
  Object.assign(look, sanitize({ ...look, ...patch }));
  try { localStorage.setItem(KEY, JSON.stringify(look)); } catch { /* private mode */ }
  apply();
  version++;
  listeners.forEach(l => l());
}

export function resetLook(): void { setLook({ ...DEFAULT_LOOK }); }

export function useLook(): Look {
  useSyncExternalStore(l => { listeners.add(l); return () => { listeners.delete(l); }; }, () => version);
  return look;
}

/** Working-state hue after the preset shift (semantic hues pass through). */
export function tintHue(h: number, semantic: boolean): number {
  return semantic ? h : (h + PRESETS[look.preset].shift + 360) % 360;
}
