import { memo, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Cpu, CornerDownRight, ChevronDown, Check, Sparkles } from 'lucide-react';
import type { ModelChoice } from '../hooks/useEra';

// ACTIVE MODEL — which brain actually handled the last request, and the switch
// for choosing a different one.
//
// WHERE IT LIVES
//   In the header, centred under the title block, in flow inside the centre
//   column. That puts it on the core's axis with the title and the command
//   pill, and means it overlaps nothing — so no panel has to reserve space for
//   it the way the old bottom-left overlay required.
//
// WHY A FALLBACK LOOKS DIFFERENT
//   A fallback that renders identically to a first choice is a fallback that
//   misleads. When the router could not use what was asked for, this shows the
//   displaced alias struck through next to the one that actually ran, in the
//   warning colour — the same amber the interface already uses for a degraded
//   state. Silence here would let a user believe they got the reasoning model
//   when they got the fast one.
//
// WHY THE SWITCHER IS THE INDICATOR
//   The thing that tells you which model is active is the thing you click to
//   change it. A separate control would have to mirror this one's state, and a
//   mirror is somewhere for the two to disagree — which, for a display whose
//   whole job is to be trusted, is the worst failure available.
//
//   The menu sends an alias and shows nothing until the backend answers. It
//   does NOT tick the clicked row optimistically: the router can refuse a
//   choice or fall back off it, and a switcher that confirmed a pin the router
//   rejected would be lying in the same way a silent fallback does.

export interface ActiveModel {
  /** The model's human name, e.g. "Claude Opus 5" — what a person is asking
   *  when they look at this. */
  label: string;
  /** Era's internal role alias, e.g. "era.reason". Secondary: it answers
   *  "which lane", not "which model". */
  alias?: string;
  /** provider/model, e.g. "claude_cli/sonnet". Shown as secondary text. */
  detail?: string;
  /** Alias the router was asked for but could not use. Empty when none. */
  fellBackFrom?: string;
  /** Why it could not be used — the tooltip. */
  fallbackReason?: string;
  /** True when the user pinned this model explicitly ("use Claude"). */
  pinned?: boolean;
}

interface Props {
  model: ActiveModel | null;
  connected: boolean;
  /** What the switcher may offer. Empty (no backend, or an unreadable
   *  registry) degrades this back to a read-only chip rather than opening an
   *  empty menu. */
  catalog?: ModelChoice[];
  /** Pinned alias, or '' for automatic. */
  selected?: string;
  onSelect?: (alias: string) => void;
}

/** Cost is only worth showing where it is not zero — every Gemini alias reports
 *  nothing, and "$0.000" next to those would read as a measured free, which is
 *  not what an absent rate card means. */
function costLabel(usd: number): string {
  if (!usd) return '';
  return usd >= 0.01 ? `~$${usd.toFixed(2)}/call` : `~$${usd.toFixed(3)}/call`;
}

function ModelIndicator({ model, connected, catalog = [], selected = '', onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const switchable = catalog.length > 0 && typeof onSelect === 'function' && connected;

  // Close on an outside click or Escape. Both, because either alone leaves a
  // way to strand an open menu over the sphere.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // A dropped connection takes the menu with it: its rows would post into a
  // dead socket and silently do nothing.
  useEffect(() => {
    if (!connected) setOpen(false);
  }, [connected]);

  const choose = useCallback(
    (alias: string) => {
      setOpen(false);
      onSelect?.(alias);
    },
    [onSelect],
  );

  // Before the backend has said anything, show the shell rather than nothing:
  // an indicator that appears and disappears is harder to read at a glance
  // than one that is always in the same place saying "—".
  const label = model?.label || (connected ? 'resolving…' : 'offline');
  const isFallback = Boolean(model?.fellBackFrom);

  const title = [
    model?.alias ? `alias ${model.alias}` : null,
    model?.detail,
    model?.pinned ? 'pinned by you — say "back to automatic" to clear' : null,
    isFallback ? `fell back from ${model?.fellBackFrom}: ${model?.fallbackReason || ''}` : null,
    switchable ? 'click to switch model' : null,
  ]
    .filter(Boolean)
    .join(' · ');

  // SIZE — a header chip, sized to sit under the title without competing with
  // it. The 44px version this replaced existed to match the command pill's
  // buttons on a shared bottom baseline; in the header that constraint is gone,
  // and matching it here would make the chip louder than the title above it.
  const chipBody = (
    <>
      <Cpu className={`h-3.5 w-3.5 shrink-0 ${isFallback ? 'text-amber-400' : 'ac-text'}`} />

      {/* Always shown now. The old `xl:` hide existed because the bottom-left
          overlay had to fit inside a 240px rail; in the header the chip is
          centred in the sphere's column and has room at every width. */}
      <span className="ac-label text-[9px] font-bold uppercase tracking-[0.18em]">Model</span>

      {isFallback && (
        <>
          <span className="t3 text-[11px] line-through decoration-amber-400/60">
            {model?.fellBackFrom}
          </span>
          <CornerDownRight className="h-3.5 w-3.5 shrink-0 text-amber-400" />
        </>
      )}

      {/* The model NAME is the headline — "Claude Opus 5", not "era.deep".
          The alias follows in smaller type: it is Era's word for a ROLE, and
          useful for debugging, but it is not the answer to "which model is
          this?" that someone glancing at the HUD is asking. */}
      <span
        className={`text-[13px] font-semibold tracking-wide ${
          isFallback ? 'text-amber-400' : 'ac-text-strong'
        }`}
      >
        {label}
      </span>

      {model?.alias && !isFallback && (
        <span className="t3 text-[9px] tabular-nums">{model.alias}</span>
      )}

      {model?.pinned && !isFallback && (
        <span
          className="ac-bg-soft ac-text rounded-full px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider"
          title='Pinned by you — say "back to automatic" to clear'
        >
          pinned
        </span>
      )}

      {switchable && (
        <ChevronDown
          className={`t3 h-3.5 w-3.5 shrink-0 transition-transform duration-200 ${
            open ? 'rotate-180' : ''
          }`}
        />
      )}
    </>
  );

  const chipClass = `flex h-8 items-center gap-2 rounded-full border px-3 glass ${
    isFallback ? 'border-amber-400/40' : 'ac-border'
  }`;

  return (
    <div ref={rootRef} className="pointer-events-auto relative">
      {switchable ? (
        <button
          type="button"
          onClick={() => setOpen(o => !o)}
          className={`${chipClass} cursor-pointer transition-colors duration-300 hover:bg-[hsl(var(--ah)_var(--as)_var(--al-fill)/0.10)]`}
          title={title || 'Active model — click to switch'}
          data-testid="model-indicator"
          aria-haspopup="listbox"
          aria-expanded={open}
        >
          {chipBody}
        </button>
      ) : (
        <div className={chipClass} title={title || 'Active model'} data-testid="model-indicator">
          {chipBody}
        </div>
      )}

      {open && (
        <div
          role="listbox"
          aria-label="Model"
          className="glass-panel absolute left-1/2 top-full z-50 mt-2 max-h-[60vh] w-[19rem] -translate-x-1/2 overflow-y-auto rounded-2xl p-1.5 text-left"
        >
          {/* AUTOMATIC comes first and is not one of the aliases: it is the
              absence of a pin, and the only way back off one by mouse. */}
          <ModelRow
            icon={<Sparkles className="h-3.5 w-3.5 shrink-0" />}
            label="Automatic"
            detail="Era routes each request"
            note="Voice stays local; planning may go to Claude"
            active={selected === ''}
            onClick={() => choose('')}
          />

          <div className="ac-border-soft my-1 border-t" />

          {catalog.map(m => (
            <ModelRow
              key={m.alias}
              icon={<Cpu className="h-3.5 w-3.5 shrink-0" />}
              label={m.label}
              detail={m.alias}
              note={
                m.available
                  ? [m.detail, costLabel(m.costPerCallUsd)].filter(Boolean).join(' · ')
                  : m.unavailableReason || 'unavailable'
              }
              active={selected === m.alias}
              disabled={!m.available}
              onClick={() => choose(m.alias)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface RowProps {
  icon: ReactNode;
  label: string;
  detail: string;
  note: string;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}

function ModelRow({ icon, label, detail, note, active, disabled = false, onClick }: RowProps) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      disabled={disabled}
      onClick={onClick}
      className={`flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors duration-200 ${
        disabled
          ? 'cursor-not-allowed opacity-40'
          : 'cursor-pointer hover:bg-[hsl(var(--ah)_var(--as)_var(--al-fill)/0.10)]'
      } ${active ? 'ac-bg-soft' : ''}`}
    >
      <span className={`mt-0.5 ${active ? 'ac-text' : 't3'}`}>{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span
            className={`truncate text-[12px] font-semibold ${active ? 'ac-text-strong' : 't1'}`}
          >
            {label}
          </span>
          <span className="t3 shrink-0 text-[9px] tabular-nums">{detail}</span>
        </span>
        {note && <span className="t3 mt-0.5 block truncate text-[10px]">{note}</span>}
      </span>
      {active && <Check className="ac-text mt-0.5 h-3.5 w-3.5 shrink-0" />}
    </button>
  );
}

// Memoized: routing changes at conversation pace, not at frame pace, so this
// subtree must not be re-rendered by the 12Hz mic ticks flowing through App.
export default memo(ModelIndicator);
