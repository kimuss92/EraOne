import type { CSSProperties } from 'react';
import { X } from 'lucide-react';

// Running-task progress. A measured value fills a hairline track with a glowing
// head and a light sweep; work without a measured value shows a travelling comet
// instead of an invented percentage. Animation is transform/opacity only.
export default function TaskProgress({ name, value, phase, stale = false, onCancel }: { name: string; value: number | null; phase: string; stale?: boolean; onCancel?: () => void }) {
  const percent = value === null ? null : Math.max(0, Math.min(100, Math.round(value)));
  const label = stale ? 'LAST KNOWN' : percent === null ? 'LIVE' : `${percent}%`;
  return <div className={`task-progress ${stale ? 'task-progress-stale' : ''} ${percent === null ? 'tp-indeterminate' : ''}`} title={`${phase}${stale ? ' · status unavailable' : ''}`}>
    <div className="tp-top">
      <i className="tp-pulse" aria-hidden="true" />
      <p className="task-progress-name" title={name}>{name}</p>
      <b className="tp-value">{label}</b>
      {onCancel && !stale && <button type="button" className="tool-cancel" title="Cancel only this Live Activity task" aria-label={`Cancel task: ${name}`} onClick={onCancel}><X className="h-3 w-3" /> Cancel</button>}
    </div>
    <div className="tp-track" role="progressbar" aria-label={name} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}
      aria-valuetext={stale ? 'Status unavailable' : percent === null ? phase : `${percent}% · ${phase}`} style={{ '--p': (percent ?? 0) / 100 } as CSSProperties}>
      {percent === null ? <span className="tp-comet" /> : <><span className="tp-fill" /><span className="tp-head"><i /></span></>}
    </div>
    <p className="tp-phase" title={phase}>{phase}</p>
  </div>;
}
