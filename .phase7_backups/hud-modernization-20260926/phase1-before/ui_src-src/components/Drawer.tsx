import type { ReactNode } from 'react';
import { X } from 'lucide-react';
import { setState } from '../lib/store';

export default function Drawer({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label={title}>
      <button className="drawer-scrim absolute inset-0" aria-label="Close" onClick={() => setState({ drawer: null })} />
      <aside className="drawer relative flex h-full w-full max-w-[460px] flex-col">
        <div className="flex h-14 shrink-0 items-center gap-2 border-b border-[var(--line)] px-4">
          <span className="micro t1 !tracking-[0.28em]">{title}</span>
          <div className="ml-auto flex items-center gap-1">
            {actions}
            <button className="icon-btn" title="Close (Esc)" onClick={() => setState({ drawer: null })}>
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto">{children}</div>
      </aside>
    </div>
  );
}
