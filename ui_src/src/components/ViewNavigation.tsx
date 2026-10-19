import { Volume2, VolumeX } from 'lucide-react';
import { useHud } from '../lib/store';
import { focusView, VIEWS, type View } from '../lib/views';
import { setSoundsEnabled, useSoundsEnabled } from '../lib/sounds';
import { setAnimationsEnabled, systemReducedMotion, useReducedMotion } from '../lib/motion';

export default function ViewNavigation() {
  const drawer = useHud(s => s.drawer);
  const sounds = useSoundsEnabled();
  const reduced = useReducedMotion();
  return <nav className="era-view-nav" aria-label="Dashboard views">
    {(Object.entries(VIEWS) as [View, string][]).map(([view, label]) => <button key={view}
      aria-pressed={view === 'overview' ? drawer === null : drawer === (view === 'tools' ? 'capabilities' : view)}
      title={`Say “Era, show me ${label.toLowerCase()}”`} onClick={() => focusView(view)}>{label}</button>)}
    <button aria-label={reduced ? 'Enable animations' : 'Reduce animations'} aria-pressed={!reduced}
      title={systemReducedMotion() ? 'Windows prefers reduced motion. Choose animations on or off for this HUD.' : 'Toggle HUD animations'}
      onClick={() => setAnimationsEnabled(reduced)}>Animations {reduced ? 'off' : 'on'}</button>
    <button aria-label={sounds ? 'Mute HUD sounds' : 'Enable quiet HUD sounds'} aria-pressed={sounds}
      onClick={() => setSoundsEnabled(!sounds)}>{sounds ? <Volume2 size={14} /> : <VolumeX size={14} />}</button>
  </nav>;
}
