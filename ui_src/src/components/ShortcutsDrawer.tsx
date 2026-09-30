import Drawer from './Drawer';
import { SHORTCUTS } from '../lib/shortcuts';

const OTHER = [
  { key: 'Ctrl K', label: 'Command palette' },
  { key: 'Space', label: 'Hold to talk while the mic is muted' },
  { key: 'Esc', label: 'Close, or stop speaking' },
];

function Row({ k, label }: { k: string; label: string }) {
  return (
    <li className="flex items-center gap-3 px-4 py-2">
      <kbd className="kbd">{k}</kbd>
      <span className="text-body t1">{label}</span>
    </li>
  );
}

export default function ShortcutsDrawer() {
  return (
    <Drawer title="KEYBOARD SHORTCUTS">
      <ul className="py-2">
        {SHORTCUTS.map(s => <Row key={s.id} k={s.key} label={s.label} />)}
        {OTHER.map(s => <Row key={s.key} k={s.key} label={s.label} />)}
      </ul>
      <p className="px-4 pb-4 text-small t3">Single keys work when you are not typing in a field.</p>
    </Drawer>
  );
}
