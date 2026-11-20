import { useEffect, useState } from 'react';
import type { Placement } from '../lib/hudPresentation';

interface Props {
  widget: { id: string } & Placement;
  title: string;
  busy: boolean;
  save: (value: Record<string, unknown>) => Promise<void>;
}

export default function LayoutEditor({ widget, title, busy, save }: Props) {
  const [draft, setDraft] = useState<Placement>(() => ({ x: widget.x, y: widget.y, w: widget.w, h: widget.h }));
  useEffect(() => { setDraft({ x: widget.x, y: widget.y, w: widget.w, h: widget.h }); }, [widget.id, widget.x, widget.y, widget.w, widget.h]);
  return <form className="era-layout-editor" onSubmit={e => { e.preventDefault(); void save({ widget: widget.id, placement: draft }); }}>
    <strong>{title}</strong>
    {(['x', 'y', 'w', 'h'] as const).map(key => <label key={key}>
      {{ x: 'Column', y: 'Row', w: 'Width', h: 'Height' }[key]}
      <input aria-label={`${title} ${key}`} type="number" step="1" required disabled={busy}
        min={key === 'w' ? 3 : key === 'h' ? 4 : 0}
        max={key === 'x' ? 12 - draft.w : key === 'y' ? 200 : key === 'w' ? 12 - draft.x : 32}
        value={draft[key]} onChange={e => setDraft(d => ({ ...d, [key]: Number(e.target.value) }))} />
    </label>)}
    <button className="mini-btn" type="submit" disabled={busy}>Apply position</button>
  </form>;
}
