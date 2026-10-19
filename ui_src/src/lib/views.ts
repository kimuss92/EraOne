import { setState } from './store';
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
