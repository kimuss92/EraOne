import { memo, useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronDown } from 'lucide-react';
import { useHud } from '../lib/store';
import { bytesRate, pct, uptime } from '../lib/format';

// Real values only, all from the backend's SystemMonitor (psutil + NVML):
// anything it cannot read arrives as null and is shown as N/A. The previous UI
// guessed the OS from the browser's user agent and counted its own uptime when
// the backend had none; both are gone.
//
// Ambient (priority 3) information: quiet by default. Values stay neutral and
// only take a semantic colour when they cross a threshold; the less-used facts
// sit behind "Details" instead of permanently taking rail height.

const DETAILS_KEY = 'era_hud_sys_details';

type Level = '' | 'warn' | 'bad';

function level(v: number | null, warn: number, bad: number): Level {
  return v === null ? '' : v >= bad ? 'bad' : v >= warn ? 'warn' : '';
}

const TONE: Record<Level, string> = { '': 't2', warn: 'text-[var(--warn)]', bad: 'text-[var(--bad)]' };

function Meter({ label, value }: { label: string; value: number | null }) {
  const v = value === null ? null : Math.max(0, Math.min(100, value));
  const lv = level(v, 70, 90);
  return (
    <div className="min-w-0 space-y-1">
      <div className="flex items-baseline justify-between gap-1">
        <span className="micro">{label}</span>
        <span className={`font-mono text-meta tabular-nums ${TONE[lv]}`}>{pct(v)}</span>
      </div>
      <div className="bar-track"><div className={`bar-fill ${lv ? `bar-${lv}` : ''}`} style={{ width: `${v ?? 0}%` }} /></div>
    </div>
  );
}

function Spark({ series }: { series: number[] }) {
  if (series.length < 2) return <div className="spark h-4" />;
  const pts = series.map((v, i) => `${(i / (series.length - 1)) * 100},${16 - (v / 100) * 15}`).join(' ');
  return (
    <svg viewBox="0 0 100 16" preserveAspectRatio="none" className="spark h-4 w-full" aria-hidden="true">
      <polyline points={pts} fill="none" stroke="var(--text-3)" strokeWidth="1" vectorEffect="non-scaling-stroke" opacity="0.7" />
    </svg>
  );
}

function Fact({ label, value, tone = 't2' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-2">
      <span className="micro">{label}</span>
      <span className={`truncate font-mono text-meta tabular-nums ${tone}`}>{value}</span>
    </div>
  );
}

function loadDetails(): boolean {
  try { return localStorage.getItem(DETAILS_KEY) === '1'; } catch { return false; }
}

function SystemPanel() {
  const m = useHud(s => s.metrics);
  const [cpuHist, setCpuHist] = useState<number[]>([]);
  const [details, setDetails] = useState(loadDetails);
  useEffect(() => {
    if (m.cpu_percent !== null) setCpuHist(h => [...h, m.cpu_percent as number].slice(-40));
  }, [m.cpu_percent]);
  useEffect(() => {
    try { localStorage.setItem(DETAILS_KEY, details ? '1' : '0'); } catch { /* private mode */ }
  }, [details]);

  const ram = m.ram_used_gb !== null && m.ram_total_gb !== null ? `${m.ram_used_gb}/${m.ram_total_gb} GB` : 'N/A';
  const vram = m.gpu_mem_used_mb !== null && m.gpu_mem_total_mb !== null
    ? `${(m.gpu_mem_used_mb / 1024).toFixed(1)}/${(m.gpu_mem_total_mb / 1024).toFixed(1)} GB`
    : 'N/A';
  const temp = level(m.gpu_temp_celsius, 75, 85);

  return (
    <section className="panel shrink-0" aria-label="System">
      <div className="panel-head">
        <span className="micro panel-title">SYSTEM</span>
        <button
          type="button"
          className="ml-auto flex items-center gap-1 rounded-[var(--r-sm)] px-1.5 py-0.5 text-micro t3 hover:bg-[var(--hover)] hover:text-[var(--text-1)]"
          aria-expanded={details}
          onClick={() => setDetails(d => !d)}
        >
          {details ? 'Less' : 'Details'}
          <ChevronDown className={`h-3 w-3 transition-transform ${details ? 'rotate-180' : ''}`} />
        </button>
      </div>
      <div className="space-y-2 p-3">
        <div className="grid grid-cols-3 gap-3">
          <Meter label="CPU" value={m.cpu_percent} />
          <Meter label="RAM" value={m.ram_percent} />
          <Meter label="GPU" value={m.gpu_usage_percent} />
        </div>
        <Spark series={cpuHist} />
        <div className="flex items-baseline justify-between gap-2 font-mono text-meta tabular-nums t3">
          <span className="flex items-center gap-2" title="Network upload / download">
            <span className="flex items-center gap-0.5"><ArrowUp className="h-3 w-3" />{bytesRate(m.net_sent_bytes_per_sec)}</span>
            <span className="flex items-center gap-0.5"><ArrowDown className="h-3 w-3" />{bytesRate(m.net_recv_bytes_per_sec)}</span>
          </span>
          <span className={temp ? TONE[temp] : ''} title="GPU temperature">
            {m.gpu_temp_celsius === null ? 'N/A' : `${m.gpu_temp_celsius}°C`}
          </span>
        </div>
        {m.wifi_connected === false && !details && (
          <p className="micro text-[var(--warn)]" role="status">WI-FI DOWN</p>
        )}
        {details && (
          <div className="space-y-1 border-t border-[var(--line)] pt-2">
            <Fact label="RAM" value={ram} />
            <Fact label="VRAM" value={vram} />
            <Fact label="UPTIME" value={uptime(m.uptime_seconds)} />
            <Fact label="PROCS" value={m.process_count === null ? 'N/A' : String(m.process_count)} />
            <Fact
              label="WI-FI"
              value={m.wifi_connected === null ? 'N/A' : m.wifi_connected ? 'UP' : 'DOWN'}
              tone={m.wifi_connected === false ? 'text-[var(--warn)]' : 't2'}
            />
            <Fact label="OS" value={m.os || 'N/A'} />
          </div>
        )}
      </div>
    </section>
  );
}

export default memo(SystemPanel);
