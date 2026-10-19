import { Fragment } from 'react';

// Small, bounded presentation tokenizer. Source is always rendered as React
// text; neither source nor generated diff is interpreted as HTML.
const TOKENS = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\/\/.*|#.*)|(\b(?:async|await|class|const|def|else|export|false|for|from|function|if|import|in|interface|let|None|null|return|self|throw|true|try|type|with|yield)\b)|(\b\d+(?:\.\d+)?\b)/g;

function highlighted(line: string) {
  const out = []; let at = 0;
  for (const match of line.matchAll(TOKENS)) {
    if (match.index! > at) out.push(<Fragment key={`t${at}`}>{line.slice(at, match.index)}</Fragment>);
    const kind = match[1] ? 'string' : match[2] ? 'comment' : match[3] ? 'keyword' : 'number';
    out.push(<span className={`cm-token-${kind}`} key={`s${match.index}`}>{match[0]}</span>);
    at = match.index! + match[0].length;
  }
  if (at < line.length) out.push(<Fragment key={`t${at}`}>{line.slice(at)}</Fragment>);
  return out;
}

export default function CodeView({ content, numbered = false, diff = false, label }: { content: string; numbered?: boolean; diff?: boolean; label: string }) {
  return <pre className="cm-code select-text" aria-label={label} tabIndex={0}><code>
    {content.split('\n').map((raw, i) => {
      const match = numbered ? /^(\d+): ?(.*)$/.exec(raw) : null;
      const line = match ? match[2] : raw;
      const kind = !diff ? undefined : line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++') || line.startsWith('diff ') ? 'meta' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'remove' : undefined;
      return <span className="cm-code-line" key={i} data-diff={kind}><span className="cm-line-number" aria-hidden="true">{match ? match[1] : i + 1}</span><span className="cm-code-text">{diff ? line : highlighted(line)}</span></span>;
    })}
  </code></pre>;
}
