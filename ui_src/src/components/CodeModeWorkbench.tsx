import { useEffect, useMemo, useState, type RefObject } from 'react';
import { ArrowLeft, ChevronDown, ChevronUp, Code2, FileCode2, GitBranch, Mic, MicOff, Minus, PanelBottom, Play, RefreshCw, Search, ShieldCheck, Square, Terminal, X } from 'lucide-react';
import { useCodeMode } from '../lib/codemode';
import type { CodeRoomPhase } from '../lib/codeTransition';
import { useDevelopment } from '../lib/development';
import { actions, api, isQtHost } from '../lib/transport';
import { useHud } from '../lib/store';
import { useBorderless, windowAction } from '../lib/windowFrame';
import CodeModelSelector from './CodeModelSelector';
import CodeModeOrb from './CodeModeOrb';
import CodeView from './CodeView';
import LiveTranscript from './LiveTranscript';
import CommandBar, { type CommandBarHandle } from './CommandBar';
import { DevelopmentContent } from './DevelopmentDrawer';
import ApprovalPrompt from './ApprovalPrompt';

type Tab = 'editor' | 'terminal' | 'preview' | 'git';
type SourcePage = { path: string; content: string; sha256: string; total_lines: number; start_line: number; truncated: boolean; redacted: boolean };
const TABS = [{ id: 'editor', label: 'Editor', Icon: FileCode2 }, { id: 'terminal', label: 'Terminal', Icon: Terminal }, { id: 'preview', label: 'Preview', Icon: Play }, { id: 'git', label: 'Git', Icon: GitBranch }] as const;

export default function CodeModeWorkbench({ phase, reduced, commandRef, onFiles, enabled }: {
  phase: CodeRoomPhase; reduced: boolean; commandRef: RefObject<CommandBarHandle | null>; onFiles: (files: File[]) => void; enabled: boolean;
}) {
  const { state, stale } = useCodeMode();
  const { jobs, stale: jobsStale } = useDevelopment();
  const link = useHud(s => s.link);
  const mic = useHud(s => s.micActive);
  const standby = useHud(s => s.standby);
  const agent = useHud(s => s.agent.state);
  const tools = useHud(s => s.tools);
  const hasConversation = useHud(s => s.conversation.some(e => e.role !== 'system') || !!s.transcript.response || !!s.transcript.user);
  const [tab, setTab] = useState<Tab>('editor');
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [selected, setSelected] = useState<{ path: string; start: number }>({ path: '', start: 1 });
  const [source, setSource] = useState<SourcePage | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceRevision, setSourceRevision] = useState(0);
  const [sourceError, setSourceError] = useState('');
  const [pages, setPages] = useState<number[]>([]);
  const [query, setQuery] = useState('');
  const [found, setFound] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [jobId, setJobId] = useState('');
  const [diffPath, setDiffPath] = useState('');
  const online = link === 'online';
  const frameless = useBorderless().active;
  const winCtl = (action: 'minimize' | 'maximize' | 'close') => isQtHost ? actions.windowControl(action) : windowAction(action);
  const titleGrip = (target: EventTarget, header: EventTarget) => target === header || Boolean((target as HTMLElement).closest('.cm-brand'));
  const busy = !!state && ['context', 'responding', 'verifying'].includes(state.phase);
  const context = state?.context || [];
  const paths = useMemo(() => [...new Set([...context.map(f => f.path), ...found, ...jobs.slice(0, 8).flatMap(j => j.files.map(f => f.path))])].slice(0, 80), [context, found, jobs]);
  const job = jobs.find(j => j.id === jobId) || jobs.find(j => j.id === state?.job_id) || jobs[0];
  const diff = job?.files.find(f => f.path === diffPath) || job?.files[0];
  // Keep explicit selection; newly discovered files never steal the reader's place.
  useEffect(() => {
    if (!selected.path && paths.length) setSelected({ path: paths[0], start: 1 });
  }, [paths, selected.path]);
  const selectedHash = context.find(f => f.path === selected.path)?.sha256;
  useEffect(() => {
    if (!selected.path || !enabled) return;
    let active = true;
    const controller = new AbortController();
    const deadline = window.setTimeout(() => controller.abort(), 8000);
    setSource(null); setSourceBusy(true); setSourceError('');
    void api<SourcePage>(`/api/codemode/source?path=${encodeURIComponent(selected.path)}&start_line=${selected.start}`, { signal: controller.signal }).then(next => {
      if (typeof next.content !== 'string' || next.path !== selected.path) throw new Error('Invalid source response');
      if (active) setSource(next);
    }).catch(e => { if (active) setSourceError(controller.signal.aborted ? 'Source request timed out. Refresh to retry.' : e instanceof Error ? e.message : String(e)); })
      .finally(() => { window.clearTimeout(deadline); if (active) setSourceBusy(false); });
    return () => { active = false; controller.abort(); window.clearTimeout(deadline); };
  }, [selected.path, selected.start, selectedHash, enabled, sourceRevision]);

  const command = async (text: string) => {
    setPending(true); setError('');
    try { await api('/api/message', { method: 'POST', body: { text } }); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setPending(false); window.dispatchEvent(new Event('era:codemode')); }
  };
  const search = async () => {
    if (!query.trim()) return;
    setSearching(true); setSourceError('');
    try {
      const map = await api<{ candidates: { path: string }[] }>('/api/development', { method: 'POST', body: { action: 'inspect', request: query } });
      const matches = map.candidates.map(c => c.path);
      setFound(matches);
      if (matches.length) { setPages([]); setSelected({ path: matches[0], start: 1 }); }
      else setSourceError('No matching source found. Describe the module or a repository-relative file path.');
    } catch (e) { setSourceError(e instanceof Error ? e.message : String(e)); }
    finally { setSearching(false); }
  };
  const consoleText = [
    `[codemode] ${stale ? 'Status unavailable; showing last received state' : state?.phase || 'Waiting for state'}`,
    `[connection] ${online ? 'Local HUD connected' : 'Reconnecting · commands resume when the link returns'}`,
    ...tools.slice(-12).map(t => `[${t.status}] ${t.label || t.tool}${t.summary ? ` · ${t.summary}` : ''}`),
    ...jobs.slice(0, 3).map(j => `\n[candidate ${j.id}] ${j.phase || j.state}${j.error ? `\n${j.error}` : ''}\n${JSON.stringify(j.checks, null, 2)}`),
    !jobs.length && !tools.length ? 'No tool or verification output yet. Submit a coding task to begin.' : '',
    jobsStale ? '[verification] Snapshot unavailable; check results may be stale.' : '',
  ].filter(Boolean).join('\n');
  const chooseFile = (path: string) => { setPages([]); setSelected({ path, start: 1 }); };
  // source.read caps responses at 10 KB. Never skip the truncated line when paging.
  const clipped = !!source && source.content.length >= 10000;
  const lastBreak = source?.content.lastIndexOf('\n') ?? -1;
  const shownSource = clipped && lastBreak >= 0 ? source!.content.slice(0, lastBreak) : source?.content || '';
  const lastLine = Number(/(?:^|\n)(\d+):[^\n]*$/.exec(shownSource)?.[1] || 0);
  const canNext = !!source && !(clipped && lastBreak < 0) && lastLine > 0 && lastLine < source.total_lines;

  return <section className={`cm-workbench cm-${phase}`} aria-label="CodeMode workbench" aria-hidden={!enabled} inert={!enabled}>
    <header className="cm-toolbar" onPointerDown={e => { if (frameless && e.button === 0 && titleGrip(e.target, e.currentTarget)) windowAction('drag'); }} onDoubleClick={e => { if (frameless && titleGrip(e.target, e.currentTarget)) windowAction('maximize'); }}>
      <div className="cm-brand"><Code2 aria-hidden="true" /><div><h1>CODE MODE</h1><span className="cm-eyebrow">ERA.ONE / Development environment</span></div></div>
      <CodeModelSelector />
      <div className="cm-toolbar-actions">
        <button className="cm-action" aria-label={standby ? 'Wake voice input' : mic ? 'Mute CodeMode microphone' : 'Enable CodeMode microphone'} aria-pressed={!standby && mic} disabled={!online} onClick={() => standby ? actions.setStandby(false) : actions.setMic(!mic)}>
          {!standby && mic ? <Mic /> : <MicOff />}{!online ? 'VOICE OFFLINE' : standby ? 'WAKE VOICE' : mic ? 'VOICE ON' : 'VOICE OFF'}
        </button>
        <button className="cm-action cm-action-primary" disabled={pending || !enabled} onClick={() => void command('exit CodeMode')}><ArrowLeft />EXIT TO MAIN</button>
        {(isQtHost || frameless) && <nav className="cm-window-controls" aria-label="Window controls">
          <button className="cm-action" aria-label="Minimize" title="Minimize" onClick={() => winCtl('minimize')}><Minus /></button>
          <button className="cm-action" aria-label="Maximize / restore" title="Maximize / restore" onClick={() => winCtl('maximize')}><Square /></button>
          <button className="cm-action" aria-label="Close window" title="Close window" onClick={() => winCtl('close')}><X /></button>
        </nav>}
      </div>
      <CodeModeOrb reduced={reduced} />
    </header>
    <div className="cm-statusbar" role="status"><span><i className="cm-status-dot" />{stale ? 'STATUS UNAVAILABLE' : state?.phase.toUpperCase().replace(/_/g, ' ') || 'CONNECTING'}</span><span>CLAUDE SUBSCRIPTION</span><span className="cm-tools" title={(state?.tools || []).join(' · ')}>{(state?.tools || []).join(' / ')}</span></div>
    {(error || state?.error) && <p role="alert" className="cm-error">{error || state?.error}</p>}
    <div className="cm-body">
      <section className="cm-panel cm-editor" aria-label="Development workspace">
        <div className="cm-tabs" role="tablist" aria-label="Code workspace">
          {TABS.map(({ id, label, Icon }) => <button key={id} id={`cm-tab-${id}`} role="tab" aria-selected={tab === id} aria-controls={`cm-pane-${id}`} tabIndex={tab === id ? 0 : -1} onClick={() => setTab(id)} onKeyDown={e => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
            e.preventDefault(); const at = TABS.findIndex(t => t.id === id);
            const next = TABS[e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : (at + (e.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length].id;
            setTab(next); document.getElementById(`cm-tab-${next}`)?.focus();
          }}><Icon aria-hidden="true" />{label}</button>)}
        </div>
        <div role="tabpanel" id="cm-pane-editor" aria-labelledby="cm-tab-editor" hidden={tab !== 'editor'} className={tab === 'editor' ? 'cm-source-layout' : undefined}>
          <aside className="cm-explorer" aria-label="Source explorer">
            <p className="cm-section-label">Project / Source</p>
            <form className="cm-source-search" onSubmit={e => { e.preventDefault(); void search(); }}>
              <input aria-label="Find project source" maxLength={2000} value={query} onChange={e => setQuery(e.target.value)} placeholder="Find a module…" />
              <button className="cm-action" disabled={searching || !query.trim()}><Search />{searching ? 'Finding…' : 'Find source'}</button>
            </form>
            <p className="cm-section-label">Context / Candidates</p>
            {paths.map(path => <button type="button" className="cm-file" key={path} aria-current={path === selected.path} onClick={() => chooseFile(path)} title={path}><FileCode2 /><span>{path}</span></button>)}
            {!paths.length && <p className="text-meta t3">Source files appear as Claude inspects your task. You can also find a module above.</p>}
          </aside>
          <div className="cm-source">
            <div className="cm-filebar"><span>{selected.path || 'Source workspace'}</span><small>READ ONLY</small>{selected.path && <button className="cm-action" title="Refresh source" aria-label="Refresh source" disabled={sourceBusy} onClick={() => setSourceRevision(n => n + 1)}><RefreshCw /></button>}</div>
            {sourceError && <p role="alert" className="cm-error">{sourceError}</p>}
            {sourceBusy ? <p className="cm-error" role="status">Reading current source…</p> : source ? <>
              <CodeView content={shownSource} numbered label={`Source ${source.path}`} />
              <div className="cm-filebar"><span title={source.sha256}>SHA {source.sha256.slice(0,12)}{source.redacted ? ' · redacted' : ''}{source.truncated ? ' · excerpt' : ''}</span><small>{source.total_lines} lines</small></div>
              {clipped && lastBreak < 0 && <p className="cm-error">This line exceeds the preview limit; showing a truncated excerpt.</p>}
              {(canNext || pages.length > 0) && <div className="cm-filebar"><button className="cm-action" disabled={!pages.length} onClick={() => { setSelected({ ...selected, start: pages[pages.length - 1] }); setPages(pages.slice(0,-1)); }}>Previous</button><button className="cm-action" disabled={!canNext} onClick={() => { setPages([...pages, selected.start]); setSelected({ ...selected, start: lastLine + 1 }); }}>Next lines</button></div>}
            </> : !sourceBusy && !selected.path && <div className="cm-empty"><Code2 /><span className="cm-section-label">Source, context, clarity</span><h2>Your coding workspace.</h2><p>Ask Claude to inspect a module, debug a failure, or build a feature. Follow the current source here and review verified changes before activation.</p><p>Try <code>“Explain core/codemode.py”</code></p></div>}
          </div>
        </div>
        <div role="tabpanel" id="cm-pane-terminal" aria-labelledby="cm-tab-terminal" hidden={tab !== 'terminal'} className={tab === 'terminal' ? 'cm-tab-scroll' : undefined}>
          <p className="cm-section-label">Live tools / Verification output</p><pre className="cm-console-output select-text">{consoleText}</pre>
          <button className="cm-action mt-4" disabled={!busy || stale || pending} onClick={() => void command('cancel coding task')}><Square />Cancel coding task</button>
          <p className="text-meta t3 mt-4">Run tests or commands through Claude. Completed checks and tool results appear here.</p>
        </div>
        <div role="tabpanel" id="cm-pane-preview" aria-labelledby="cm-tab-preview" hidden={tab !== 'preview'} className={tab === 'preview' ? 'cm-tab-scroll' : undefined}>
          <p className="cm-section-label">Candidate preview / Review &amp; activation</p><DevelopmentContent embedded />
        </div>
        <div role="tabpanel" id="cm-pane-git" aria-labelledby="cm-tab-git" hidden={tab !== 'git'} className={tab === 'git' ? 'cm-source' : undefined} style={tab === 'git' ? { flex: 1 } : undefined}>
          <div className="cm-filebar"><GitBranch size={13} /><span>Candidate diffs</span>{jobs.length > 0 && <select className="field min-w-0 max-w-[50%]" aria-label="Candidate to inspect" value={job?.id || ''} onChange={e => { setJobId(e.target.value); setDiffPath(''); }}>{jobs.map(j => <option key={j.id} value={j.id}>{j.request.slice(0,60)}</option>)}</select>}</div>
          {job?.files.length ? <><div className="cm-filebar flex-wrap">{job.files.map(f => <button className="cm-action" key={f.path} aria-pressed={diff?.path === f.path} onClick={() => setDiffPath(f.path)}>{f.path}</button>)}</div><CodeView content={diff?.diff || ''} diff label={`Candidate diff ${diff?.path || ''}`} /><div className="cm-filebar"><span>{jobsStale ? 'Verification status unavailable' : job.state.replace(/_/g,' ')}</span><button className="cm-action" onClick={() => setTab('preview')}><ShieldCheck />Review candidate</button></div></> : <div className="cm-empty"><GitBranch /><h2>Changes, made visible.</h2><p>Candidate diffs appear after Claude stages an improvement. Open Preview to review checks and approve the exact candidate.</p></div>}
        </div>
      </section>
      <section className="cm-panel cm-assistant" aria-label="Claude coding assistant">
        <div className="cm-assistant-head"><div>CLAUDE / ASSISTANT</div><span>{stale ? 'STATUS UNAVAILABLE' : busy ? 'WORKING' : 'CONTEXT CONNECTED'}</span></div>
        <div className="cm-chat">
          {!hasConversation && <div className="cm-chat-empty"><span className="cm-section-label">Think it. Say it. Build it.</span><h2>What are we building?</h2><p>Your code, tools and conversation in one place. Speak naturally, or give Claude a focused task below.</p><div className="cm-suggestions">{['Explain this function', 'Debug this module', 'Run the focused tests'].map(text => <button key={text} onClick={() => commandRef.current?.setText(text)}>{text}</button>)}</div></div>}
          <LiveTranscript workbench />
        </div>
        <ApprovalPrompt />
        <div className="cm-compose"><CommandBar ref={commandRef} onFiles={onFiles} coding /></div>
      </section>
    </div>
    <section className="cm-panel cm-console" aria-label="Verification console">
      <button className="cm-console-head" aria-expanded={consoleOpen} aria-controls="cm-console-output" onClick={() => setConsoleOpen(!consoleOpen)}><span><Terminal />CONSOLE <span className="t3">/ {jobs.length} candidates · {tools.filter(t => t.status === 'running').length} active tools</span></span><span><PanelBottom />{consoleOpen ? <ChevronDown /> : <ChevronUp />}</span></button>
      {consoleOpen && <pre className="cm-console-output select-text" id="cm-console-output" role="log" aria-label="Latest verification output">{consoleText}</pre>}
    </section>
    <footer className="cm-foot"><span>{online ? 'LOCAL CONNECTION' : 'RECONNECTING'} / {agent.toUpperCase().replace(/_/g,' ')}</span><span>CLAUDE SUBSCRIPTION · VERIFY → REVIEW → ACTIVATE</span></footer>
  </section>;
}
