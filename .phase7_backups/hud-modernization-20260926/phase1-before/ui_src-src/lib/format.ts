import type { AgentState } from './types';

export const STATE_LABEL: Record<AgentState, string> = {
  idle: 'READY',
  wake_detected: 'WAKE DETECTED',
  listening: 'LISTENING',
  transcribing: 'TRANSCRIBING',
  routing: 'ROUTING',
  thinking: 'THINKING',
  tool_running: 'EXECUTING',
  synthesizing: 'SYNTHESIZING',
  speaking: 'SPEAKING',
  interrupted: 'INTERRUPTED',
  complete: 'COMPLETE',
  error: 'ERROR',
  offline: 'OFFLINE',
  standby: 'STANDBY',
};

// Accent per state (hue, saturation). Thinking is gold and standby red by the
// user's explicit direction (kept from the previous HUD); error is a restrained
// amber, offline a desaturated steel.
export const STATE_HUE: Record<AgentState, { h: number; s: number }> = {
  idle: { h: 195, s: 78 },
  wake_detected: { h: 188, s: 95 },
  listening: { h: 187, s: 92 },
  transcribing: { h: 192, s: 88 },
  routing: { h: 205, s: 85 },
  thinking: { h: 45, s: 90 },
  tool_running: { h: 198, s: 92 },
  synthesizing: { h: 180, s: 80 },
  speaking: { h: 172, s: 84 },
  interrupted: { h: 28, s: 80 },
  complete: { h: 160, s: 70 },
  error: { h: 14, s: 78 },
  offline: { h: 212, s: 14 },
  standby: { h: 355, s: 70 },
};

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? 'N/A' : `${Math.round(v)}%`;
}

export function bytesRate(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'N/A';
  if (v < 1024) return `${v} B/s`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB/s`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB/s`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function uptime(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return 'N/A';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : `${h}h ${m.toString().padStart(2, '0')}m`;
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(now - ts));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/** "models/gemini-3.1-flash-live-preview" -> "Gemini 3.1 Flash Live (preview)". */
export function prettyModel(id: string | null | undefined): string {
  if (!id) return 'N/A';
  const base = id.replace(/^models\//, '');
  const preview = /-preview$/.test(base);
  const words = base
    .replace(/-preview$/, '')
    .split('-')
    .map(w => (/^\d/.test(w) ? w : w.charAt(0).toUpperCase() + w.slice(1)));
  return words.join(' ') + (preview ? ' (preview)' : '');
}
