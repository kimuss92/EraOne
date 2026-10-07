import type { CSSProperties } from 'react';
export default function TaskProgress({ name, value, phase, stale = false }: { name: string; value: number | null; phase: string; stale?: boolean }) {
  const percent = value === null ? null : Math.max(0, Math.min(100, Math.round(value)));
  return <div className={`task-progress ${stale ? 'task-progress-stale' : ''}`} title={`${phase}${stale ? ' · status unavailable' : ''}`}>
    <p className="task-progress-name" title={name}>{name}</p>
    <div className={`liquid-bar ${percent === null ? 'liquid-indeterminate' : ''}`} role="progressbar" aria-label={name} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} aria-valuetext={stale ? 'Status unavailable' : percent === null ? phase : `${percent}% · ${phase}`} style={{ '--fill': `${percent ?? 35}%` } as CSSProperties}>
      <div className="liquid-fill"><i /><i /></div><span className="liquid-label">{stale ? 'Last known state' : percent === null ? 'Working' : `${percent}%`}</span>
    </div>
  </div>;
}
