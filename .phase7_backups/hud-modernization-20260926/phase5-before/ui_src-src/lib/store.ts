import { useSyncExternalStore } from 'react';
import type {
  ActiveModel,
  AgentState,
  Approval,
  ConnectionInfo,
  ContextCard,
  ConversationEntry,
  DrawerId,
  Latency,
  ModelChoice,
  Source,
  SystemMetrics,
  TimelineItem,
  ToolRun,
  Transcript,
} from './types';

// ── The HUD store ───────────────────────────────────────────────────────
// A tiny external store read through useSyncExternalStore + selectors, so a
// component re-renders only when the slice it reads changes. High-rate signals
// (Era's voice amplitude, the mic level, render FPS) never enter it: they live
// in `live` below and are read by animation loops directly.

export interface HudState {
  link: 'connecting' | 'online' | 'reconnecting';
  rttMs: number | null;
  agent: { state: AgentState; label: string; seq: number };
  connection: ConnectionInfo;
  standby: boolean;
  micActive: boolean;
  voiceActive: boolean;
  transcript: Transcript;
  tools: ToolRun[];
  timeline: TimelineItem[];
  cards: ContextCard[];
  sources: Source[];
  conversation: ConversationEntry[];
  metrics: SystemMetrics;
  model: ActiveModel | null;
  catalog: ModelChoice[];
  selectedModel: string;
  latency: Latency | null;
  memory: { retrieved_total: number; last_count: number; last_at: number | null };
  toolPulse: { seq: number; category: string };
  approval: Approval | null;
  theme: 'dark' | 'light';
  showLeft: boolean;
  showRight: boolean;
  overlay: 'left' | 'right' | null;
  drawer: DrawerId;
  palette: boolean;
  backendStartedAt: number | null;
}

const EMPTY_METRICS: SystemMetrics = {
  cpu_percent: null,
  ram_percent: null,
  ram_used_gb: null,
  ram_total_gb: null,
  net_sent_bytes_per_sec: null,
  net_recv_bytes_per_sec: null,
  wifi_connected: null,
  gpu_usage_percent: null,
  gpu_temp_celsius: null,
  gpu_mem_used_mb: null,
  gpu_mem_total_mb: null,
  uptime_seconds: null,
  process_count: null,
};

function loadTheme(): 'dark' | 'light' {
  try {
    return localStorage.getItem('era_theme') === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function loadPanel(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === '1';
  } catch {
    return fallback;
  }
}

let state: HudState = {
  link: 'connecting',
  rttMs: null,
  agent: { state: 'offline', label: 'Connecting', seq: 0 },
  connection: { kind: 'connecting', label: 'Connecting', status: '' },
  standby: false,
  micActive: true,
  voiceActive: false,
  transcript: { user: '', userFinal: false, response: '', responding: false },
  tools: [],
  timeline: [],
  cards: [],
  sources: [],
  conversation: [],
  metrics: EMPTY_METRICS,
  model: null,
  catalog: [],
  selectedModel: '',
  latency: null,
  memory: { retrieved_total: 0, last_count: 0, last_at: null },
  toolPulse: { seq: 0, category: '' },
  approval: null,
  theme: loadTheme(),
  showLeft: loadPanel('era_hud_left', true),
  showRight: loadPanel('era_hud_right', true),
  overlay: null,
  drawer: null,
  palette: false,
  backendStartedAt: null,
};

const listeners = new Set<() => void>();

export function getState(): HudState {
  return state;
}

export function setState(patch: Partial<HudState> | ((s: HudState) => Partial<HudState>)): void {
  const next = typeof patch === 'function' ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach(l => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useHud<T>(selector: (s: HudState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(state));
}

// ── Side rails ──────────────────────────────────────────────────────────
// Wide enough → rails dock beside the core (showLeft/showRight, persisted).
// Narrower → a rail opens only on demand as an overlay, one at a time, so the
// centre is never covered by default.
export const DOCK_QUERY = { left: '(min-width: 1024px)', right: '(min-width: 1280px)' } as const;
export type Side = keyof typeof DOCK_QUERY;

export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    l => {
      const mq = window.matchMedia(query);
      mq.addEventListener('change', l);
      return () => mq.removeEventListener('change', l);
    },
    () => window.matchMedia(query).matches,
  );
}

export function togglePanel(side: Side): void {
  if (window.matchMedia(DOCK_QUERY[side]).matches) {
    setState(s => (side === 'left' ? { showLeft: !s.showLeft } : { showRight: !s.showRight }));
  } else {
    setState(s => ({ overlay: s.overlay === side ? null : side }));
  }
}

// Wall clock (epoch seconds) for relative times ("3m ago"). One shared 1 s
// ticker that runs only while something reads it, so only the tiny components
// that render a relative time re-render — not the panels around them.
let nowSec = Date.now() / 1000;
let nowTimer = 0;
const nowListeners = new Set<() => void>();

function subscribeNow(l: () => void): () => void {
  nowListeners.add(l);
  if (!nowTimer) {
    nowTimer = window.setInterval(() => {
      nowSec = Date.now() / 1000;
      nowListeners.forEach(f => f());
    }, 1000);
  }
  return () => {
    nowListeners.delete(l);
    if (!nowListeners.size) { window.clearInterval(nowTimer); nowTimer = 0; }
  };
}

export function useNow(): number {
  return useSyncExternalStore(subscribeNow, () => nowSec);
}

// High-rate, non-React signals. Written by the transport, read per frame.
export const live = {
  amplitude: 0,
  bands: new Float32Array(8),
  micLevel: 0,
  fps: 0,
};

export const EMPTY = { metrics: EMPTY_METRICS };
