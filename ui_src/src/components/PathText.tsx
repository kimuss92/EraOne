import { memo, type SyntheticEvent } from 'react';
import { actions } from '../lib/transport';
import { notify } from '../lib/notifications';
import { shortPath } from '../lib/format';

// Any Windows path inside HUD text becomes a button that opens it. The backend
// (/api/reveal, mode "open") only accepts a path the HUD itself published,
// opens folders in Explorer and documents with their app, and merely REVEALS
// anything that would run code — so a click can never launch a program.
//
// A quoted path may contain spaces ("C:\My Files\a.txt"); an unquoted one ends
// at whitespace, and trailing sentence punctuation is not part of it.

const PATH_RE = /(["'`])([A-Za-z]:[\\/][^"'`<>|\r\n]*?)\1|([A-Za-z]:[\\/][^\s"'`<>|]*)/g;
const TRAILING = /[.,;:!?)\]}]+$/;

type Part = string | { path: string };

export function splitPaths(text: string): Part[] {
  const out: Part[] = [];
  let last = 0;
  for (const m of text.matchAll(PATH_RE)) {
    const quoted = m[2];
    let path = quoted ?? m[3];
    let end = (m.index ?? 0) + m[0].length;
    if (!quoted) {
      const trail = path.match(TRAILING)?.[0] ?? '';
      path = path.slice(0, path.length - trail.length);
      end -= trail.length;
    }
    const start = (m.index ?? 0) + (quoted ? 1 : 0);
    if (path.length < 4) continue;
    if (start > last) out.push(text.slice(last, start));
    out.push({ path });
    last = quoted ? end - 1 : end;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const stop = (e: SyntheticEvent) => e.stopPropagation();

export function openPath(path: string): void {
  actions.openPath({ path }).catch((e: { message?: string }) =>
    notify({ kind: 'warn', key: `open:${path}`, title: 'Could not open', message: `${path} — ${e.message || 'unavailable'}` }),
  );
}

function PathText({ text, short = false, format }: {
  text: string;
  /** Show long paths shortened (the full path stays in the tooltip). */
  short?: boolean;
  /** Applied to the non-path text only (e.g. emoji stripping). */
  format?: (s: string) => string;
}) {
  if (!text) return null;
  const parts = splitPaths(text);
  if (parts.length === 1 && typeof parts[0] === 'string') return <>{format ? format(text) : text}</>;
  return (
    <>
      {parts.map((p, i) =>
        typeof p === 'string' ? (
          format ? format(p) : p
        ) : (
          <button
            key={i}
            type="button"
            className="path-link"
            title={`Open ${p.path}`}
            onClick={e => { stop(e); openPath(p.path); }}
            onKeyDown={stop}
          >
            {short ? shortPath(p.path) : p.path}
          </button>
        ),
      )}
    </>
  );
}

export default memo(PathText);
