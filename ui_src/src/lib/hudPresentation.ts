/** Presentation-only commands: never execute a tool or change stored results. */
export function resultIntent(text: string): 'hide' | 'show' | null {
  const t = text.trim().toLowerCase().replace(/[.!]+$/g, '').replace(/^(?:hey\s+)?era[,\s]+/, '').replace(/^please\s+/, '').replace(/\s+please$/, '');
  if (/^(?:hide|dismiss|close|clear) (?:this|that|it|(?:the |this |that )?(?:result|results|card|popup|widget|email|information)(?: card| widget| popup)?)$/.test(t)) return 'hide';
  if (/^(?:show|bring back|restore) (?:the |my |last |that )?(?:result|results|card|popup|email|information)(?: again)?$/.test(t)) return 'show';
  return null;
}

export function freshResult(timestamp: number, now: number): boolean {
  return Number.isFinite(timestamp) && timestamp <= now + 5 && now - timestamp < 60;
}

export function isResultSurface(card: { type: string; subtitle: string; payload?: Record<string, unknown> }): boolean {
  if (['email', 'web', 'file', 'memory', 'vision', 'generic'].includes(card.type)) return true;
  // Routine volume/play/open operations should not cover the sphere with popups.
  return /^(?:read|list|info|status|search|inspect|summary|describe)$/.test(String(card.payload?.action ?? ''));
}

/** Docks stay inside one viewport. Order/visibility are backend-owned. */
export function dockPlacement(id: string, ordered: string[]): { x: number; y: number; w: number; h: number } {
  if (id === 'core') return { x: 3, y: 0, w: 6, h: 28 };
  if (id === 'weather') return { x: 9, y: 0, w: 3, h: 5 };
  if (id === 'system') return { x: 0, y: 21, w: 3, h: 7 };
  if (id === 'awareness') return { x: 9, y: 21, w: 3, h: 7 };
  const slots = [
    { x: 0, y: 0, w: 3, h: 8 }, { x: 9, y: 5, w: 3, h: 5 },
    { x: 0, y: 8, w: 3, h: 7 }, { x: 9, y: 10, w: 3, h: 6 },
    { x: 9, y: 16, w: 3, h: 5 }, { x: 0, y: 15, w: 3, h: 6 },
  ];
  // Preserve semantic starting docks; reordered widgets take each other's slots.
  const ids = ordered.filter(key => !['core', 'weather', 'system', 'awareness'].includes(key));
  return slots[Math.max(0, ids.indexOf(id)) % slots.length];
}

export type Placement = { x: number; y: number; w: number; h: number };
export function dropPlacement(p: Placement, x: number, y: number): Placement {
  return { ...p, x: Math.max(0, Math.min(12 - p.w, Math.round(x))), y: Math.max(0, Math.min(200, Math.round(y))) };
}
