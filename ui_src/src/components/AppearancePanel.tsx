import type { CSSProperties } from 'react';
import { RotateCcw, X } from 'lucide-react';
import { PRESETS, resetLook, setLook, useLook, type Density, type Look } from '../lib/appearance';
import { setAnimationsEnabled, useReducedMotion } from '../lib/motion';
import { setSoundsEnabled, useSoundsEnabled } from '../lib/sounds';
import { appWindow, setBorderless, useBorderless } from '../lib/windowFrame';
import { setState, useHud } from '../lib/store';

// "Customize": every visual knob of the HUD in one place. Viewer-side only
// (localStorage); panel position and size live in Arrange, which the backend
// stores. Nothing here changes what Era does or the data on screen.

type NumKey = 'uiScale' | 'coreScale' | 'glow' | 'speed' | 'panelOpacity';
const SLIDERS: { key: NumKey; label: string; min: number; max: number; step: number }[] = [
  { key: 'uiScale', label: 'HUD scale', min: 0.8, max: 1.25, step: 0.05 },
  { key: 'coreScale', label: 'Sphere size', min: 0.7, max: 1.4, step: 0.05 },
  { key: 'glow', label: 'Glow', min: 0.4, max: 1.6, step: 0.05 },
  { key: 'speed', label: 'Animation speed', min: 0.4, max: 1.6, step: 0.05 },
  { key: 'panelOpacity', label: 'Panel opacity', min: 0.35, max: 1, step: 0.05 },
];

function Toggle({ label, on, set }: { label: string; on: boolean; set: (v: boolean) => void }) {
  return <label className="era-look-toggle">
    <span>{label}</span>
    <input type="checkbox" role="switch" checked={on} onChange={e => set(e.target.checked)} />
    <i aria-hidden="true" />
  </label>;
}

export default function AppearancePanel({ onClose }: { onClose: () => void }) {
  const look = useLook();
  const reduced = useReducedMotion();
  const sounds = useSoundsEnabled();
  const borderless = useBorderless();
  const theme = useHud(s => s.theme);
  const set = (patch: Partial<Look>) => setLook(patch);
  return <section className="era-look-panel" aria-label="Customize the HUD" onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } }}>
    <header>
      <span className="era-eyebrow">CUSTOMIZE</span>
      <button aria-label="Reset to defaults" title="Reset to defaults" onClick={resetLook}><RotateCcw /></button>
      <button aria-label="Close" onClick={onClose}><X /></button>
    </header>
    <div className="era-look-presets" role="radiogroup" aria-label="Colour">
      {(Object.keys(PRESETS) as Look['preset'][]).map(id => <button key={id} role="radio" aria-checked={look.preset === id}
        style={{ '--swatch': `hsl(${PRESETS[id].accent} ${Math.max(55, PRESETS[id].sat)}% 62%)` } as CSSProperties}
        onClick={() => set({ preset: id })}><i />{PRESETS[id].label}</button>)}
    </div>
    {SLIDERS.map(s => <label key={s.key} className="era-look-slider">
      <span>{s.label}<b>{Math.round(look[s.key] * 100)}%</b></span>
      <input type="range" min={s.min} max={s.max} step={s.step} value={look[s.key]}
        onChange={e => set({ [s.key]: Number(e.target.value) } as Partial<Look>)} />
    </label>)}
    <div className="era-look-seg" role="radiogroup" aria-label="Detail">
      <span>Detail</span>
      {(['low', 'medium', 'high'] as Density[]).map(d => <button key={d} role="radio" aria-checked={look.density === d} onClick={() => set({ density: d })}>{d}</button>)}
    </div>
    <Toggle label="Tool constellation" on={look.constellation} set={v => set({ constellation: v })} />
    <Toggle label="Labels & tool counts" on={look.labels} set={v => set({ labels: v })} />
    <Toggle label="Animations" on={!reduced} set={setAnimationsEnabled} />
    <Toggle label="Interface sounds" on={sounds} set={v => setSoundsEnabled(v)} />
    {appWindow && <Toggle label="Borderless window" on={borderless.enabled} set={setBorderless} />}
    <Toggle label="Light theme" on={theme === 'light'} set={v => setState({ theme: v ? 'light' : 'dark' })} />
    <p className="era-look-hint">Move and resize panels with <b>Arrange</b>. Saved on this computer.</p>
  </section>;
}
