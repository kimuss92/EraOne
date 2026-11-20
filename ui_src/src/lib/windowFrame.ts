import { useSyncExternalStore } from 'react';
import { api, isQtHost } from './transport';

// Borderless window (backend: app/hud/window_frame.py). Only for the Chromium
// --app window the launcher opens — its toolbar is hidden — never a normal
// browser tab or the Qt host (already frameless). The page measures the title
// bar and the invisible resize border from inside (outer vs inner size) and the
// backend clips them off with a window region. Re-applied on resize, focus and
// fullscreen changes; turning it off restores the normal frame.

const KEY = 'era_hud_borderless';
export const appWindow = !isQtHost && typeof window.toolbar !== 'undefined' && !window.toolbar.visible;
let enabled = true;
try { enabled = localStorage.getItem(KEY) !== '0'; } catch { /* private mode */ }
let active = false;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach(cb => cb());

export function useBorderless(): { enabled: boolean; active: boolean } {
  const v = useSyncExternalStore(cb => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => (enabled ? 1 : 0) + (active ? 2 : 0));
  return { enabled: Boolean(v & 1), active: Boolean(v & 2) };
}

/** Title bar + invisible resize border, in physical pixels. A region does not
 *  change the window's size, so this stays measurable after clipping. */
function inset() {
  const dpr = window.devicePixelRatio || 1;
  const side = Math.max(0, Math.round((window.outerWidth - window.innerWidth) / 2 * dpr));
  const top = Math.max(0, Math.round((window.outerHeight - window.innerHeight) * dpr) - side);
  return { top, side };
}

let timer = 0;
function sync(): void {
  if (!appWindow) return;
  window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    const on = enabled && !document.fullscreenElement;
    const { top, side } = inset();
    if (on && top === 0 && side === 0) return; // no frame to clip (e.g. kiosk)
    api<{ ok: boolean; borderless?: boolean }>('/api/window', { method: 'POST', body: on ? { action: 'frame', top, side } : { action: 'frame', enabled: false } })
      .then(r => { active = Boolean(r.borderless); emit(); })
      // 409 = the HUD is not the foreground window right now; the next focus retries.
      .catch(() => {});
  }, 140);
}

export function setBorderless(on: boolean): void {
  enabled = on;
  try { localStorage.setItem(KEY, on ? '1' : '0'); } catch { /* private mode */ }
  emit();
  sync();
}

export function windowAction(action: 'minimize' | 'maximize' | 'close' | 'drag'): void {
  void api('/api/window', { method: 'POST', body: { action } }).then(() => { if (action === 'maximize') sync(); }).catch(() => {});
}

if (appWindow) {
  window.addEventListener('resize', sync);
  window.addEventListener('focus', sync);
  document.addEventListener('fullscreenchange', sync);
  window.setTimeout(sync, 400);
}
