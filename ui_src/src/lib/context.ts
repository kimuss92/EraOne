import { shortPath } from './format';
import type { ContextCard } from './types';

// Pure helpers for the Context panel: roadmap categories, raw-text tidying and
// grouping of repeated results. The raw card is never altered — the panel shows
// the tidy form by default and the raw text when a card is expanded.

/** Backend category (card.icon, from tool_adapter.meta) -> scannable label. */
const CATEGORY_LABEL: Record<string, string> = {
  files: 'FILE',
  web: 'WEB',
  browser: 'WEB',
  apps: 'APP',
  system: 'SYSTEM',
  voice: 'SYSTEM',
  memory: 'MEMORY',
  vision: 'VISION',
  media: 'MEDIA',
  email: 'EMAIL',
  message: 'MESSAGE',
  task: 'TASK',
  weather: 'WEATHER',
  cm: 'CM',
};

export function categoryOf(c: ContextCard): string {
  return CATEGORY_LABEL[c.icon] || CATEGORY_LABEL[c.type] || 'TOOL';
}

/** The tool that produced a card ("tool:file_controller" -> "file_controller"). */
export function toolOf(c: ContextCard): string {
  return c.source?.startsWith('tool:') ? c.source.slice(5) : c.source || '';
}

// Emoji in tool output are decoration from the spoken reply; the HUD has its
// own icon system. Long Windows paths read better shortened (full path stays
// in the expanded, raw view and in tooltips).
const PICTOGRAPH = /\p{Extended_Pictographic}️?/gu;
const WIN_PATH = /[A-Za-z]:\\[^\s"'<>|]+/g;

export function tidy(text: string): string {
  return stripDecor(text).replace(WIN_PATH, m => shortPath(m)).trim();
}

/** tidy() minus path shortening and trimming — for text around path buttons. */
export function stripDecor(text: string): string {
  return (text || '').replace(PICTOGRAPH, '').replace(/\s{2,}/g, ' ');
}

export interface CardGroup {
  key: string;
  category: string;
  /** newest occurrence — what the card shows */
  latest: ContextCard;
  /** older occurrences of the same result, newest first */
  earlier: ContextCard[];
}

/** Same kind + same title = the same thing seen again (e.g. "AUDIO" set three
 *  times): one card with a count, placed at its latest use. Input newest first. */
export function groupCards(newestFirst: ContextCard[]): CardGroup[] {
  const byKey = new Map<string, CardGroup>();
  const out: CardGroup[] = [];
  for (const c of newestFirst) {
    const key = `${c.type}|${c.icon}|${c.title.trim().toLowerCase()}`;
    const g = byKey.get(key);
    if (g) {
      g.earlier.push(c);
    } else {
      const ng = { key, category: categoryOf(c), latest: c, earlier: [] };
      byKey.set(key, ng);
      out.push(ng);
    }
  }
  return out;
}
