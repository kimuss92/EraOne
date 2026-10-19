import { useEffect, useRef, useState } from 'react';
import { Pin, X, Sparkles } from 'lucide-react';
import { useHud } from '../lib/store';
import { freshResult, isResultSurface, resultIntent } from '../lib/hudPresentation';
import { ResultCard } from './ContextPanel';
import type { ContextCard } from '../lib/types';

export default function ResultSurface() {
  const cards = useHud(s => s.cards);
  const lastUser = useHud(s => [...s.conversation].reverse().find(e => e.role === 'user'));
  const latest = [...cards].reverse().find(isResultSurface);
  const [card, setCard] = useState<ContextCard | null>(null);
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const held = hovered || focused;
  const pinGuard = useRef(false);
  const surface = useRef<HTMLElement>(null);
  const setPin = (value: boolean) => { pinGuard.current = value; setPinned(value); if (value) setLeaving(false); };
  const [leaving, setLeaving] = useState(false);
  const seen = useRef('');
  const dismiss = () => { pinGuard.current = false; setPinned(false); seen.current = latest?.id ?? seen.current; setLeaving(true); };
  useEffect(() => {
    if (!latest || latest.id === seen.current || pinGuard.current) return;
    seen.current = latest.id;
    if (freshResult(latest.timestamp, Date.now() / 1000)) {
      setCard(latest); setPin(false); setHovered(false); setFocused(false); setLeaving(false);
    }
  }, [latest, pinned]);
  useEffect(() => {
    const intent = resultIntent(lastUser?.text ?? '');
    if (intent === 'hide') dismiss();
    if (intent === 'show' && latest) { setCard(latest); setLeaving(false); setPin(false); }
    // Only a NEW user utterance may control the current surface.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastUser?.id]);
  useEffect(() => {
    if (!card || pinned || held || leaving) return;
    const timer = window.setTimeout(() => {
      // A pin or reading focus acquired at the expiry boundary wins over the timer.
      if (!pinGuard.current && !surface.current?.matches(':hover') && !surface.current?.contains(document.activeElement)) dismiss();
    }, 15000);
    return () => window.clearTimeout(timer);
  }, [card, pinned, held, leaving]);
  useEffect(() => {
    if (!leaving) return;
    const timer = window.setTimeout(() => { setCard(null); setLeaving(false); }, 220);
    return () => window.clearTimeout(timer);
  }, [leaving]);
  useEffect(() => {
    const onShow = () => { if (latest) { setCard(latest); setLeaving(false); setPin(false); } };
    window.addEventListener('era:show-result', onShow);
    return () => window.removeEventListener('era:show-result', onShow);
  }, [latest]);
  if (!card) return null;
  return <aside className={`era-result-surface ${leaving ? 'is-leaving' : ''}`} data-result-id={card.id} ref={surface} aria-label="Latest result" aria-live="polite"
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)} onFocus={() => setFocused(true)}
    onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setFocused(false); }}
    onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); dismiss(); } }}>
    <header><Sparkles /><span>INTELLIGENCE / {card.type.toUpperCase()}</span>
      <button title={pinned ? 'Unpin result' : 'Pin result'} aria-label={pinned ? 'Unpin result' : 'Pin result'} aria-pressed={pinned} onClick={() => setPin(!pinned)}><Pin /></button>
      <button aria-label="Hide result" title="Hide result" onClick={dismiss}><X /></button></header>
    <div className="era-result-content custom-scrollbar"><ResultCard card={card} /></div>
    <footer>{pinned ? 'Pinned until you hide it' : 'Fades after 15 seconds · hover or focus to keep reading'}<button onClick={dismiss}>Hide</button></footer>
  </aside>;
}
