import { useEffect, useState } from 'react';
import { Check, Cpu, Minus } from 'lucide-react';
import Drawer from './Drawer';
import { CATEGORY_ICON } from './ActivityPanel';
import { api } from '../lib/transport';

// The live tool registry (app/tool_declarations.py via /api/capabilities),
// grouped into skills by category. A tool added on the backend appears here
// with no HUD change. Integrations report only whether they are configured.

interface Caps {
  count: number;
  tools: { name: string; label: string; category: string; description: string }[];
  skills: { id: string; tools: string[]; count: number }[];
  integrations: { id: string; name: string; connected: boolean; available?: boolean }[];
}

export default function CapabilitiesDrawer() {
  const [caps, setCaps] = useState<Caps | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    api<Caps>('/api/capabilities').then(setCaps).catch(e => setErr(String(e.message || e)));
  }, []);
  const byName = new Map((caps?.tools || []).map(t => [t.name, t]));
  return (
    <Drawer title={`CAPABILITIES${caps ? ` · ${caps.count} TOOLS` : ''}`}>
      {err && <p className="px-4 pt-3 text-meta text-[var(--warn)]">{err}</p>}
      {caps && (
        <>
          <section className="border-b border-[var(--line)] px-4 py-3">
            <h3 className="micro mb-2 t2">INTEGRATIONS</h3>
            <div className="grid grid-cols-2 gap-1.5">
              {caps.integrations.map(i => (
                <div key={i.id} className="flex items-center gap-2 text-small">
                  {i.connected ? <Check className="h-3.5 w-3.5 text-[var(--ok)]" /> : <Minus className="h-3.5 w-3.5 t3" />}
                  <span className={i.connected ? 't1' : 't3'}>{i.name}</span>
                  {!i.connected && <span className="micro ml-auto">NOT CONNECTED</span>}
                </div>
              ))}
            </div>
          </section>
          {caps.skills.map(skill => {
            const Icon = CATEGORY_ICON[skill.id] || Cpu;
            return (
              <section key={skill.id} className="border-b border-[var(--line)] px-4 py-3">
                <h3 className="micro mb-2 flex items-center gap-2 t2"><Icon className="h-3.5 w-3.5 ac-text" />{skill.id.toUpperCase()} · {skill.count}</h3>
                <div className="space-y-1.5">
                  {skill.tools.map(n => {
                    const t = byName.get(n);
                    return (
                      <div key={n}>
                        <div className="flex items-baseline gap-2">
                          <span className="font-mono text-small t1">{n}</span>
                          <span className="micro">{t?.label}</span>
                        </div>
                        {t?.description && <p className="text-meta leading-snug t3">{t.description}</p>}
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </>
      )}
    </Drawer>
  );
}
