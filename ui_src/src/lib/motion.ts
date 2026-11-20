import { useSyncExternalStore } from 'react';

const KEY = 'era_hud_motion';
const media = window.matchMedia('(prefers-reduced-motion: reduce)');
const listeners = new Set<() => void>();
let override: string | null = null;
try { override = localStorage.getItem(KEY); } catch { /* private mode */ }
// Follow the OS by default. An explicit in-app choice takes precedence.
export const systemReducedMotion = () => media.matches;
export const reducedMotion = () => override === 'full' ? false : override === 'reduced' ? true : systemReducedMotion();
function publish() {
  document.documentElement.dataset.motion = reducedMotion() ? 'reduced' : 'full';
  listeners.forEach(cb => cb());
}
media.addEventListener('change', publish);
publish();
export function setAnimationsEnabled(enabled: boolean) {
  override = enabled ? 'full' : 'reduced';
  try { localStorage.setItem(KEY, override); } catch { /* private mode */ }
  publish();
}
export function useReducedMotion() {
  return useSyncExternalStore(cb => { listeners.add(cb); return () => { listeners.delete(cb); }; }, reducedMotion);
}
