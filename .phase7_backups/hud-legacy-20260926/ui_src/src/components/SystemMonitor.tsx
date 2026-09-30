import { memo, useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, Cpu, MemoryStick, Gauge as GaugeIcon } from 'lucide-react';
import type { SystemMetrics } from '../hooks/useEra';

interface Props {
  muted: boolean;
  metrics: SystemMetrics;
}

function fmtClock(totalSeconds: number) {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`;
}

function fmtBytes(bytesPerSec: number | null): string {
  if (bytesPerSec === null || bytesPerSec === undefined) return '—';
  if (bytesPerSec < 1024) return `${bytesPerSec}B/s`;
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)}K/s`;
  return `${(bytesPerSec / (1024 * 1024)).toFixed(1)}M/s`;
}

function detectOS() {
  const ua = navigator.userAgent;
  if (ua.includes('Windows')) return 'Windows';
  if (ua.includes('Mac')) return 'macOS';
  if (ua.includes('Linux')) return 'Linux';
  return navigator.platform || 'Unknown';
}

// Load color turns amber past 70% and red past 90% — pressure reads at a
// glance without reading the numbers (color + the number, never color only).
function loadClass(pct: number | null): string {
  if (pct === null) return 't3';
  if (pct >= 90) return 'text-red-400';
  if (pct >= 70) return 'text-amber-400';
  return 'ac-text';
}

// One metric row, Apple-style: a small icon tile (the visual anchor every
// Apple list row hangs off), the label, the big number, and a hairline
// progress track underneath.
function Gauge({ label, pct, icon: Icon }: { label: string; pct: number | null; icon: typeof Cpu }) {
  const v = pct !== null ? Math.round(pct) : null;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2.5">
        <span className="ac-bg-soft ac-text flex h-7 w-7 shrink-0 items-center justify-center rounded-lg">
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
        <span className="t2 text-[12px] font-medium">{label}</span>
        <span className={`ml-auto font-mono text-[15px] font-bold leading-none tabular-nums ${loadClass(pct)}`}>
          {v !== null ? `${v}%` : '—'}
        </span>
      </div>
      <div className="h-[5px] w-full overflow-hidden rounded-full" style={{ background: 'var(--track)' }}>
        <div
          className={v !== null && v >= 90 ? 'h-full rounded-full bg-red-400' : v !== null && v >= 70 ? 'h-full rounded-full bg-amber-400' : 'ac-fill h-full rounded-full'}
          style={{ width: `${v ?? 0}%`, transition: 'width 600ms ease-out' }}
        />
      </div>
    </div>
  );
}

function Fact({ label, value, valueClass = 't1' }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="t3 text-[10px] font-medium">{label}</span>
      <span className={`font-mono text-[10px] font-semibold tabular-nums ${valueClass}`}>{value}</span>
    </div>
  );
}

// SYSTEM section — a FLAT section now, not a card: it lives inside the single
// unified left glass panel (App.tsx), one sheet of glass per the reference
// design, with hairline dividers separating sections instead of separate
// floating cards.
function SystemMonitor({ muted, metrics }: Props) {
  const os = useMemo(() => detectOS(), []);

  // Session uptime fallback, self-contained (used only when the backend does
  // not report uptime_seconds).
  const [uptime, setUptime] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setUptime(s => s + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const uptimeVal = metrics.uptime_seconds ?? uptime;
  const gpuTemp = metrics.gpu_temp_celsius;

  return (
    <div className="px-4 pb-4 pt-3">
      <div className="space-y-3.5">
        <Gauge label="CPU" pct={metrics.cpu_percent} icon={Cpu} />
        <Gauge label="RAM" pct={metrics.ram_percent} icon={MemoryStick} />
        <Gauge label="GPU" pct={metrics.gpu_usage_percent} icon={GaugeIcon} />
      </div>

      {/* NET: up/down split beats a single merged number for diagnosis */}
      <div className="mt-4 flex items-center justify-between border-t pt-3 border-line-soft">
        <span className="t3 text-[10px] font-medium">Network</span>
        <span className="flex items-center gap-2.5 font-mono text-[10px] tabular-nums">
          <span className="t2 flex items-center gap-0.5">
            <ArrowUp className="h-2.5 w-2.5" />{fmtBytes(metrics.net_sent_bytes_per_sec)}
          </span>
          <span className="t2 flex items-center gap-0.5">
            <ArrowDown className="h-2.5 w-2.5" />{fmtBytes(metrics.net_recv_bytes_per_sec)}
          </span>
        </span>
      </div>

      <div className="mt-2.5 grid grid-cols-2 gap-x-5 gap-y-1.5 border-t pt-2.5 border-line-soft">
        <Fact label="Temp" value={gpuTemp !== null ? `${gpuTemp}°C` : '—'} valueClass={gpuTemp !== null && gpuTemp >= 80 ? 'text-red-400' : 't1'} />
        <Fact label="Procs" value={metrics.process_count !== null ? String(metrics.process_count) : '—'} />
        <Fact label="Uptime" value={fmtClock(uptimeVal)} />
        <Fact label="OS" value={os} />
        <Fact label="Mute" value={muted ? 'Yes' : 'No'} valueClass={muted ? 'text-amber-400' : 'text-emerald-400'} />
        <Fact label="Wi-Fi" value={metrics.wifi_connected === null ? '—' : metrics.wifi_connected ? 'Up' : 'Down'} valueClass={metrics.wifi_connected === false ? 'text-amber-400' : 't1'} />
      </div>
    </div>
  );
}

export default memo(SystemMonitor);
