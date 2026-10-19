import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { useCodeMode } from '../lib/codemode';
import { api } from '../lib/transport';

export default function CodeModelSelector() {
  const { state, stale } = useCodeMode();
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState(0);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const busy = !!state && ['context', 'responding', 'verifying'].includes(state.phase);
  const disabled = stale || !state || busy || pending;
  const models = state?.models || ['sonnet','opus','haiku'];
  const label = (model: string) => `Claude ${model[0]?.toUpperCase() || ''}${model.slice(1)}`;
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown',outside); document.addEventListener('keydown',escape);
    return () => { document.removeEventListener('pointerdown',outside); document.removeEventListener('keydown',escape); };
  }, [open]);
  const choose = async (model: string) => {
    if (disabled || !model) return;
    setOpen(false); trigger.current?.focus(); setPending(true); setError('');
    try { await api('/api/message', { method: 'POST', body: { text: `change model to ${model}` } }); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setPending(false); window.dispatchEvent(new Event('era:codemode')); }
  };
  return <div ref={root} className="seg min-w-0 code-model-picker">
    <span className="micro">{state?.enabled ? 'CODE MODE' : 'CODING MODEL'}</span>
    <button ref={trigger} type="button" role="combobox" className="code-model-trigger" aria-label="CodeMode model" aria-expanded={open} aria-controls={`${id}-list`} aria-haspopup="listbox" aria-activedescendant={open ? `${id}-${focused}` : undefined} disabled={disabled}
      onBlur={event => { if (!root.current?.contains(event.relatedTarget as Node)) setOpen(false); }}
      onClick={() => { setFocused(Math.max(0,models.indexOf(state?.model || 'sonnet'))); setOpen(!open); }}
      onKeyDown={event => {
        if (models.length && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
          event.preventDefault();
          const selected = Math.max(0,models.indexOf(state?.model || 'sonnet'));
          setFocused(event.key === 'Home' ? 0 : event.key === 'End' ? models.length-1 : open ? (focused + (event.key === 'ArrowDown' ? 1 : -1) + models.length)%models.length : selected);
          setOpen(true);
        } else if (open && ['Enter',' '].includes(event.key)) { event.preventDefault(); void choose(models[focused]); }
        else if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); setOpen(false); }
        else if (event.key === 'Tab') setOpen(false);
        else if (open && event.key.length === 1) {
          const index = models.findIndex(m => m.toLowerCase().startsWith(event.key.toLowerCase()));
          if (index >= 0) setFocused(index);
        }
      }}>
      <span>{label(state?.model || 'sonnet')}</span><ChevronDown size={12} aria-hidden="true" />
    </button>
    {open && <div id={`${id}-list`} className="code-model-menu" role="listbox" aria-label="Coding models">
      {models.map((model,index) => <button type="button" key={model} id={`${id}-${index}`} role="option" aria-selected={model === state?.model} data-focused={focused === index} tabIndex={-1}
        onPointerDown={event => event.preventDefault()} onPointerMove={() => setFocused(index)} onClick={() => void choose(model)}>{label(model)}</button>)}
    </div>}
    {error && <span role="alert" className="text-micro text-[var(--bad)]">{error}</span>}
  </div>;
}
