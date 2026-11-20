import { useSyncExternalStore } from 'react';
import { getState, subscribe } from './store';
import type { SystemMetrics } from './types';

// Rolling telemetry history for the graphs: one sample per REAL system_metrics
// update from the backend (psutil + NVML), kept for the session so a graph that
// mounts later (drawer, re-arranged panel) still shows the recent past.
// null stays null — a gap in the line, never an invented value.

export interface Sample {
  t: number;
  cpu: number | null;
  ram: number | null;
  gpu: number | null;
  netIn: number | null;
  netOut: number | null;
}

const MAX = 180;
export const history: Sample[] = [];
const listeners = new Set<() => void>();
let version = 0;
let lastMetrics: SystemMetrics | null = null;

subscribe(() => {
  const s = getState();
  const m = s.metrics;
  if (m === lastMetrics) return;
  lastMetrics = m;
  if (s.link !== 'online' || m.cpu_percent === null) return;
  history.push({
    t: Date.now(), cpu: m.cpu_percent, ram: m.ram_percent, gpu: m.gpu_usage_percent,
    netIn: m.net_recv_bytes_per_sec, netOut: m.net_sent_bytes_per_sec,
  });
  if (history.length > MAX) history.splice(0, history.length - MAX);
  version++;
  listeners.forEach(l => l());
});

/** Re-renders the caller once per new sample. */
export function useTelemetryVersion(): number {
  return useSyncExternalStore(l => { listeners.add(l); return () => { listeners.delete(l); }; }, () => version);
}
