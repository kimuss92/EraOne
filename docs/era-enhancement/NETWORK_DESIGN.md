# Local network awareness — 2026-10-13

Authorized Phase 6 continuation. Track local interface/link observations through
Windows NotifyIpInterfaceChange, with bounded event coalescing and 15-second
reconciliation. Query only adapter name, interface index and link-up status.
Do not collect IP/MAC addresses, SSIDs, traffic, credentials or connection contents.
No ping/HTTP/DNS probe, route change, adapter restart or network reconfiguration.

Reuse the existing process-scoped observer lifecycle and failure handling. Keep
callbacks short: signal a worker, which reads a fresh snapshot outside the callback.
Cancellation must occur on the worker, not inside the notification callback, and
callback references stay alive until cancellation succeeds. Failed registration
falls back to explicitly labeled reconciliation-only coverage. Read failure clears
evidence; expired evidence is unknown. Duplicate starts are refused; stop reports
failure if native registration could not be released.

Interface indexes identify the current observation, not permanent device identity.
Adapter rename/removal replaces the complete snapshot. Unknown index is explicit.
Link up is not verified internet access: expose internet reachability as unknown
in this observer and preserve the existing separate reachability probe.

HUD receives detached revisioned snapshots/events, excluding unchanged heartbeats
from the timeline. Reasoning gets a bounded summary of adapter/link state with
freshness and coverage; it must query live tools before confirming connectivity.

Alternatives: continuous psutil polling adds scans and delays manual changes;
NLM connectivity events would add COM subscription complexity; active internet
probes add traffic and conflate remote availability with local adapter state.
IP Helper events plus reconciliation are the chosen narrow increment.

Verification: fake link changes/removal, unknown indexes, malformed data, stale
snapshots, publication failures, duplicate startup, fallback and shutdown;
native read-only registration/read/cancellation probe; existing foreground/process/
media/state/model ownership and bridge regressions; UI build/TypeScript, compile,
lint, manifest and Doctor. Live adapter-change acceptance remains pending.

Primary contracts:
https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-notifyipinterfacechange
https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-cancelmibchangenotify2

Native probe required a Windows friendly-alias fallback for interface indexes:
https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-convertinterfacealiastoluid
https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-convertinterfaceluidtoindex
