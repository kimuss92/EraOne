import { useState } from 'react';
import { useCodeMode } from '../lib/codemode';
import { api } from '../lib/transport';
import CodeModelSelector from './CodeModelSelector';

export default function CodeModeControls() {
  const { state, stale } = useCodeMode();
  const [error, setError] = useState('');
  const command = async (text: string) => {
    try { setError(''); await api('/api/message', { method: 'POST', body: { text } }); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  return <section className="dev-map" aria-label="CodeMode">
    <div className="flex items-center gap-2 ac-text"><span className="micro">CODE MODE</span><span className="chip chip-dim">{stale ? 'status unavailable' : state?.phase || 'loading'}</span></div>
    <p className="text-meta t3 mt-2">Claude subscription{state?.profile ? ` · ${state.profile}` : ''}. Changes require verification and your final approval.</p>
    <CodeModelSelector />
    <p className="text-meta t3 mt-2">Tools: {(state?.tools || []).join(' · ')}</p>
    <div className="flex gap-2 mt-2"><button className={`mini-btn ${state && !state.enabled && !stale ? 'cm-entry-ready' : ''}`} disabled={stale || !state} onClick={() => void command(state?.enabled ? 'exit CodeMode' : 'enter CodeMode')}>{state?.enabled ? 'Exit CodeMode' : 'Enter CodeMode'}</button>{state?.enabled && <button className="mini-btn" onClick={() => void command('cancel coding task')}>Cancel coding task</button>}</div>
    {(error || state?.error) && <p role="alert" className="text-meta text-[var(--warn)] mt-2">{error || state?.error}</p>}
    {!!state?.context.length && <details className="mt-2"><summary className="text-meta t3 cursor-pointer">Current source context · {state.context.length} files</summary>{state.context.map(file => <p className="text-meta font-mono t3 break-all mt-1" key={file.path}>{file.path} · {file.sha256.slice(0, 12)}{file.truncated ? ' · excerpt' : ''}</p>)}</details>}
  </section>;
}
