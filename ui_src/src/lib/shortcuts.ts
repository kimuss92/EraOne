// Single source of truth for the HUD's single-key shortcuts: App.tsx binds
// them, the command palette shows them as hints, the Shortcuts drawer lists
// them. Keys are matched case-insensitively and never fire while typing or
// with Ctrl/⌘/Alt held (Ctrl+K stays the palette).

export type ShortcutId =
  | 'mic' | 'standby' | 'ask' | 'conversation' | 'schedule' | 'capabilities'
  | 'diagnostics' | 'settings' | 'left' | 'right' | 'fullscreen'
  | 'restart' | 'exit' | 'help';

export interface Shortcut {
  id: ShortcutId;
  key: string;
  label: string;
  /** Destructive: the key only arms a confirmation, it never acts alone. */
  confirms?: boolean;
}

export const SHORTCUTS: Shortcut[] = [
  { id: 'mic', key: 'M', label: 'Mute / unmute the microphone' },
  { id: 'standby', key: 'S', label: 'Standby / wake' },
  { id: 'ask', key: '/', label: 'Focus the command bar' },
  { id: 'conversation', key: 'C', label: 'Conversation history' },
  { id: 'schedule', key: 'T', label: 'Scheduled tasks' },
  { id: 'capabilities', key: 'P', label: 'Capabilities' },
  { id: 'diagnostics', key: 'D', label: 'Diagnostics' },
  { id: 'settings', key: ',', label: 'Settings' },
  { id: 'left', key: '[', label: 'Live activity panel' },
  { id: 'right', key: ']', label: 'Context panel' },
  { id: 'fullscreen', key: 'F', label: 'Fullscreen' },
  { id: 'restart', key: 'R', label: 'Restart ERA ONE (asks first)', confirms: true },
  { id: 'exit', key: 'X', label: 'Exit ERA ONE (asks first)', confirms: true },
  { id: 'help', key: '?', label: 'This list' },
];

/** Shortcut key for an id, for palette hints. */
export function keyFor(id: ShortcutId): string {
  return SHORTCUTS.find(s => s.id === id)?.key ?? '';
}

/** Resolve a keydown to a shortcut id, or null. Letters need Shift released. */
export function matchShortcut(e: KeyboardEvent): ShortcutId | null {
  if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return null;
  const k = e.key;
  const isLetter = k.length === 1 && k.toLowerCase() !== k.toUpperCase();
  if (isLetter && e.shiftKey) return null;
  const want = k.toUpperCase();
  return SHORTCUTS.find(s => s.key === want)?.id ?? null;
}
