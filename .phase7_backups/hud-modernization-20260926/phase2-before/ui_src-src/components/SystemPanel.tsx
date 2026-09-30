import { memo, useEffect, useState } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { useHud } from '../lib/store';
import { bytesRate, pct, uptime } from '../lib/format';

// Real values only, all from the backend's SystemMonitor (psutil + NVML):
// anything it cannot read arrives as null and is shown as N/A. The previous UI
// guessed the OS from the browser's user agent and counted its own uptime when
// the backend had none; both are gone.

function Bar({ label, value, hint }: { label: string; value: number | null; hint?: string }) {
  const v = value === null ? null : Math.max(0, Math.min(100, value));
  const tone = v === null ? '' : v >= 90 ? 'bar-bad' : v >= 70 ? 'bar-warn' : '';
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <span className="micro">{label}</span>
        <span className="font-mono text-meta tabular-nums t1">
          {pct(v)}
          {hint && <span className="t3 ml-1 text-micro">{hint}</span>}
        </span>
      </div>
      <div className="bar-track"><div className={`bar-fill ${tone}`} style={{ width: `${v ?? 0}%` }} /></div>
    </div>
  );
}

function Spark({ series }: { series: number[] }) {
  if (series.length < 2) return <div className="h-6" />;
  const pts = series.map((v, i) => `${(i / (series.length - 1)) * 100},${24 - (v / 100) * 22}`).join(' ');
  return (
    <svg viewBox="0 0 100 24" preserveAspectRatio="none" className="h-6 w-full" aria-hidden="true">
      <polyline points={pts} fill="none" stroke="var(--text-3)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Fact({ label, value, tone = 't1' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="micro">{label}</span>
      <span className={`truncate font-mono text-meta tabular-nums ${tone}`}>{value}</span>
    </div>
  );
}

function SystemPanel() {
  const m = useHud(s => s.metrics);
  const [cpuHist, setCpuHist] = useState<number[]>([]);
  useEffect(() => {
    if (m.cpu_percent !== null) setCpuHist(h => [...h, m.cpu_percent as number].slice(-40));
  }, [m.cpu_percent]);

  const ram = m.ram_used_gb !== null && m.ram_total_gb !== null ? `${m.ram_used_gb}/${m.ram_total_gb} GB` : undefined;
  const vram = m.gpu_mem_used_mb !== null && m.gpu_mem_total_mb !== null
    ? `${(m.gpu_mem_used_mb / 1024).toFixed(1)}/${(m.gpu_mem_total_mb / 1024).toFixed(1)} GB`
    : undefined;

  return (
    <section className="panel shrink-0" aria-label="System">
      <div className="panel-head"><span className="micro panel-title">SYSTEM</span></div>
      <div className="space-y-2.5 px-3 pb-3 pt-2">
        <Bar label="CPU" value={m.cpu_percent} />
        <Spark series={cpuHist} />
        <Bar label="RAM" value={m.ram_percent} hint={ram} />
        <Bar label="GPU" value={m.gpu_usage_percent} hint={vram} />
        <div className="flex items-center justify-between border-t border-[var(--line)] pt-2">
          <span className="micro">NETWORK</span>
          <span className="flex gap-3 font-mono text-meta tabular-nums t2">
            <span className="flex items-center gap-0.5"><ArrowUp className="h-3 w-3" />{bytesRate(m.net_sent_bytes_per_sec)}</span>
            <span className="flex items-center gap-0.5"><ArrowDown className="h-3 w-3" />{bytesRate(m.net_recv_bytes_per_sec)}</span>
          </span>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          <Fact
            label="GPU TEMP"
            value={m.gpu_temp_celsius === null ? 'N/A' : `${m.gpu_temp_celsius}°C`}
            tone={m.gpu_temp_celsius !== null && m.gpu_temp_celsius >= 80 ? 'text-[var(--bad)]' : 't1'}
          />
          <Fact label="PROCS" value={m.process_count === null ? 'N/A' : String(m.process_count)} />
          <Fact label="UPTIME" value={uptime(m.uptime_seconds)} />
          <Fact
            label="WI-FI"
            value={m.wifi_connected === null ? 'N/A' : m.wifi_connected ? 'UP' : 'DOWN'}
            tone={m.wifi_connected === false ? 'text-[var(--warn)]' : 't1'}
          />
        </div>
        <Fact label="OS" value={m.os || 'N/A'} />
      </div>
    </section>
  );
}

export default memo(SystemPanel);
