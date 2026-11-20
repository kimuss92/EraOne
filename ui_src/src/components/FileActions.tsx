import { useEffect, useState } from 'react';
import { api } from '../lib/transport';

export function FileActions({ path }: { path: string }) {
  const [error, setError] = useState('');
  const open = async (reveal: boolean) => {
    const code = /\.(py|tsx?|jsx?|json|ya?ml|toml|md|css|html|rs|go|java|cpp|c|cs|sh)$/i.test(path);
    try {
      setError('');
      await api('/api/reveal', { method: 'POST', body: { path, mode: reveal ? 'reveal' : code ? 'edit' : 'open' } });
      window.dispatchEvent(new Event('era-file-opened'));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  return <div className="text-meta break-all mt-1">
    <span className="font-mono t3" title={path}>{path}</span>
    <span className="inline-flex gap-2 ml-2"><button type="button" className="ac-text" onClick={() => void open(false)}>Open File</button><button type="button" className="ac-text" onClick={() => void open(true)}>Show in Folder</button></span>
    {error && <span role="alert" className="text-[var(--warn)]">{error}</span>}
  </div>;
}

export function RecentFiles() {
  const [files, setFiles] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    const load = () => { void api<{ files: string[] }>('/api/recent-files').then(result => { if (active) setFiles(result.files); }).catch(() => {}); };
    load(); window.addEventListener('era-file-opened', load);
    return () => { active = false; window.removeEventListener('era-file-opened', load); };
  }, []);
  return files.length ? <details className="mt-2"><summary className="micro cursor-pointer">Recent files · {files.length}</summary>{files.map(path => <FileActions key={path} path={path} />)}</details> : null;
}
