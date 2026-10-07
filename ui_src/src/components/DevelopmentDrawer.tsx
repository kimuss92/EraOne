import { useState } from 'react';
import { Check, Code2, FileCode, Search, ShieldCheck, RefreshCw } from 'lucide-react';
import Drawer from './Drawer';
import { api } from '../lib/transport';
import { useDevelopment, working, type Job } from '../lib/development';
export default function DevelopmentDrawer() {
  const { jobs, stale, refresh } = useDevelopment();
  const [request, requestSet] = useState(''), [error, errorSet] = useState(''), [busy, busySet] = useState(false);
  const [reviewed, reviewedSet] = useState<Record<string, string>>({});
  const [map, mapSet] = useState<{ candidates: { path: string; editable: boolean }[]; skills: { path: string }[]; wiring: Record<string, string>; toolchain?: { claude_cli: boolean; pytest: boolean; ruff: boolean; node: boolean; npm: boolean } } | null>(null);
  const mutate = async (body: unknown) => {
    busySet(true); errorSet('');
    try { await api('/api/development', { method: 'POST', body }); refresh(); }
    catch (e) { errorSet(e instanceof Error ? e.message : String(e)); } finally { busySet(false); }
  };
  const inspect = async () => {
    busySet(true); errorSet('');
    try { mapSet(await api('/api/development', { method: 'POST', body: { action: 'inspect', request } })); }
    catch (e) { errorSet(e instanceof Error ? e.message : String(e)); } finally { busySet(false); }
  };
  const checkLabel = (j: Job) => j.checks.passed ? 'Verification passed' : Object.keys(j.checks).length ? 'Verification needs attention' : 'Verification pending';
  return <Drawer title="DEVELOPMENT">
    <div className="p-4 space-y-4">
      <div className="dev-intro"><Code2 className="h-5 w-5 ac-text" /><div><h2 className="text-body t1">Build, verify, then activate</h2><p className="text-meta t3 mt-1">Claude plans in a source copy. Your running code changes only after approval.</p></div></div>
      <form className="space-y-2" onSubmit={e => { e.preventDefault(); void mutate({ action: 'propose', request }); }}>
        <textarea aria-label="Feature request" required maxLength={8000} rows={3} value={request} onChange={e => { requestSet(e.target.value); mapSet(null); }} placeholder="Describe what Era should fix or add…" className="field w-full p-3 text-small resize-y" />
        <div className="flex gap-2"><button type="button" className="mini-btn" disabled={busy || !request.trim()} onClick={() => void inspect()}><Search className="h-3.5 w-3.5" />Find source</button><button className="mini-btn dev-primary" disabled={busy || jobs.some(working) || !request.trim()}><Code2 className="h-3.5 w-3.5" />Stage improvement</button></div>
      </form>
      {map && <div className="dev-map"><span className="micro">SOURCE MAP</span>{map.candidates.slice(0, 8).map(c => <p key={c.path} className="text-meta font-mono t2 break-all mt-1">{c.path}{!c.editable && <span className="t3"> · review only</span>}</p>)}{!map.candidates.length && <p className="text-meta t2 mt-1">New capability: create features/&lt;name&gt;.py and a focused test. The feature runner supplies registration.</p>}{map.toolchain && <p className="text-meta t3 mt-2">Claude CLI {map.toolchain.claude_cli ? "found · authentication checked on staging" : "missing · install or set CLAUDE_BIN"}. Test tools {map.toolchain.pytest && map.toolchain.ruff ? "found" : "missing · install project requirements"}. Frontend tools {map.toolchain.node && map.toolchain.npm ? "found" : "missing · install Node LTS"}.</p>}{map.skills.map(s => <p key={s.path} className="text-meta ac-text mt-1">Skill · {s.path}</p>)}</div>}
      {(stale || error) && <p role="alert" className="text-meta text-[var(--warn)]">{error || 'Status unavailable. Showing the last received development state.'}</p>}
      {!jobs.length && <p className="text-meta t3 py-4">Your verified improvements and recovery history appear here.</p>}
      {jobs.map(j => <article className="dev-job space-y-3" key={j.id}>
        <div><div className="flex items-center gap-2"><span className={`chip ${j.state === 'failed' ? 'chip-bad' : j.state === 'activated' ? 'chip-ok' : 'chip-live'}`}>{j.state.replace(/_/g, ' ')}</span><span className="ml-auto text-micro font-mono t3">Attempt {j.attempt || 1}/2</span></div><h3 className="text-small t1 mt-2 break-words">{j.request}</h3><p className="text-meta t3 mt-1">{j.phase || j.state.replace(/_/g, ' ')}</p></div>
        {j.requires_restart && <p className="text-meta text-[var(--warn)]">Engine restart required to load these Python changes.</p>}
        {j.error && <p role="alert" className="text-meta text-[var(--bad)] break-words">{j.error}</p>}
        {j.plan && <p className="text-meta t2">{j.plan}</p>}
        <details><summary className="dev-summary"><ShieldCheck className="h-3.5 w-3.5" />{checkLabel(j)} · {j.files.length} files</summary><div className="flex flex-wrap gap-1 mt-2">{['syntax', 'lint', 'tests', 'feature_contract'].map(k => <span className={`chip ${j.checks[k] === true ? 'chip-ok' : 'chip-dim'}`} key={k}>{k.replace(/_/g, ' ')} {j.checks[k] === true ? '✓' : '—'}</span>)}</div><details className="mt-2"><summary className="text-meta t3 cursor-pointer">Full check and diagnosis report</summary><pre className="dev-code">{JSON.stringify(j.checks, null, 2)}</pre></details>{j.files.map(f => <details key={f.path} className="mt-2"><summary className="dev-summary break-all"><FileCode className="h-3.5 w-3.5 shrink-0" />{f.path}</summary><pre className="dev-code">{f.diff}</pre></details>)}</details>
        {j.state === 'awaiting_approval' && <><label className="flex gap-2 text-meta t2"><input type="checkbox" checked={reviewed[j.id] === j.approval} onChange={e => reviewedSet({ ...reviewed, [j.id]: e.target.checked ? j.approval : '' })} />I reviewed the changes and check results.</label><div className="flex gap-2"><button className="mini-btn dev-primary" disabled={busy || stale || reviewed[j.id] !== j.approval} onClick={() => void mutate({ action: 'approve', id: j.id, approval: j.approval, confirmed: true })}><Check className="h-3.5 w-3.5" />Approve and activate</button><button className="mini-btn" disabled={busy} onClick={() => void mutate({ action: 'reject', id: j.id })}>Reject</button></div></>}
        {j.state === 'failed' && <button className="mini-btn" disabled={busy || jobs.some(working)} onClick={() => void mutate({ action: 'retry', id: j.id })}><RefreshCw className="h-3.5 w-3.5" />Retry from current source</button>}
      </article>)}
      <p className="text-micro t3">Progress reflects completed verification checkpoints, not estimated elapsed time. Candidate repair is bounded to two attempts. Approved feature code runs on this machine.</p>
    </div>
  </Drawer>;
}
