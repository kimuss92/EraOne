import { getState, setState } from './store';
import { cue } from './sounds';
import type { DrawerId } from './types';

export const VIEWS = {
  overview: 'Overview', telemetry: 'System telemetry', activity: 'Live activity',
  memory: 'Memory', context: 'Context', schedule: 'Schedule', diagnostics: 'Diagnostics', tools: 'Tools',
  telegram: 'Telegram', development: 'Development',
} as const;
export type View = keyof typeof VIEWS;

export function focusView(value: unknown): boolean {
  if (typeof value !== 'string' || !Object.prototype.hasOwnProperty.call(VIEWS, value)) return false;
  const view = value as View;
  const drawer: DrawerId = view === 'overview' ? null : view === 'tools' ? 'capabilities' : view;
  setState({ focusView: view, drawer, palette: false, overlay: null });
  cue('focus');
  return true;
}

// Close only the named drawer, preserving any different open view.
export function setPanelVisibility(drawer: unknown, visible: unknown): boolean {
  const drawers = ['diagnostics', 'settings', 'development', 'capabilities', 'conversation', 'telegram'];
  if (typeof drawer !== 'string' || !drawers.includes(drawer) || typeof visible !== 'boolean') return false;
  if (visible) setState({ drawer: drawer as DrawerId, palette: false, overlay: null });
  else if (getState().drawer === drawer) setState({ drawer: null, focusView: 'overview' });
  return true;
}
