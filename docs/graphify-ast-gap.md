# graphify AST gap: function-local lazy imports produce no cross-file edges

**Status:** open (graphifyy 0.8.45, observed 2026-10-07)
**Symptom:** a module that IS called in production shows up in the graph with zero
inbound call edges, which makes a live feature look dead.

## What the AST pass misses

`graphify.extract` resolves module-level imports only. When a call site uses a
**function-local import** plus a module-qualified attribute call, no cross-file
edge is emitted — not an `imports` edge from the file node, not a `calls` edge
from the function.

Missed pattern:

```python
async def _route_text_through_lanes(self, text: str) -> None:
    import lane_dispatch                       # local import
    decision = await asyncio.to_thread(lane_dispatch.handle, text, dispatch=False)
```

Resolved pattern (works today):

```python
import lane_dispatch          # module level
from lane_router import route
```

## Concrete instances in this repo

| Caller | Callee | Status in graph |
|---|---|---|
| `app/era_live/cm_flow.py:128,354` `_route_text_through_lanes()` / `_run_cm_flow_job()` | `lane_dispatch.handle()` | **repaired 2026-10-07** — both `calls` edges now EXTRACTED 1.0 |
| `main.py:2779` (startup warm-up block) | `lane_router.warm_up()` | **repaired 2026-10-07** — `main --calls--> lane_router_warm_up` present |

## Why it matters

The doc-derived node `lane_dispatch.py and lane_router.py unwired (zero references)`
(from `LUNAR_HANDOFF.md`) was tagged AMBIGUOUS 0.2 by the semantic pass — correct
honesty, but the graph *structure* appeared to corroborate a false claim, so the
node surfaced under "Surprising Connections" and misled a live investigation.
Corrected in `LUNAR_HANDOFF.md` on 2026-10-07; the structural gap is repaired
(see remediation 2 — applied the same day).

## Remediation options

1. **Upstream fix** — teach `graphify.extract` to resolve `import x` inside a
   function body and attribute calls `x.f()` across files (emit `imports` from the
   enclosing file and `calls` from the enclosing function).
2. **Workaround here** — **APPLIED 2026-10-07.** A scoped deep semantic pass over
   `cm_flow.py` + `main.py` added the missing cross-file `calls` edges (they are
   literal imports + literal calls in source, so EXTRACTED 1.0, not inferred).
   68 edges recovered; graph now 20,332 nodes / 37,906 edges. See
   "Hazards hit while applying it" below before repeating this.
3. **Convention** — keep heavy/optional imports at module level when the module is
   small; reserve function-local imports for genuinely optional dependencies.

## Hazards hit while applying remediation 2 (read before re-running)

Two graphify behaviours nearly corrupted the graph:

1. **`build_merge` replace-by-`source_file`.** Any `source_file` present in the new
   chunk makes `build_merge` DROP every base node/edge with that file — then only
   what you re-supply survives. A semantic subagent that emits *collaborator* nodes
   (`era_dispatch_run_task` with `source_file=era_dispatch.py`, …) therefore deleted
   794 unrelated nodes in one merge (era_dispatch 89, file_controller 84, …).
   **Safe pattern:** ship only nodes whose `source_file` is a file you are fully
   re-extracting, plus nodes for files with zero base content. For everyone else,
   emit the *edge* only (edge `source_file` = the originating file) and let the
   target reuse its existing base node.
2. **AST node IDs depend on the path form you pass.** `extract([Path('main.py')])`
   yields `eraone_main_eralive_…` (absolute parent dir) while the graph's base IDs
   are `main_eralive_…`; `cm_flow.py`'s file node differs the same way. Replacing a
   file's nodes without matching the original path form orphans its whole subtree.
   **Safe pattern:** when patching edges into an existing graph, add edges only —
   never re-supply a file's AST nodes unless you have verified the IDs line up
   (`set(old_ids) & set(new_ids)` should be ~100%).

Also: remap semantic-agent IDs that don't match base
(`main_main`→`main`, `hud_events_hud`→`hud_events`, missing `_py_app_text_intents`
segment) *before* merging, and drop edges whose endpoints don't exist.

## Verification

```
graphify path "lane_dispatch" "era_dispatch"
  -> lane_dispatch.py --imports [EXTRACTED]--> era_dispatch.py   (1 hop, correct direction)

# inbound side (repaired 2026-10-07), all `calls` EXTRACTED 1.0:
#   era_live_cm_flow_cmflowmixin_route_text_through_lanes --calls--> lane_dispatch_handle
#   era_live_cm_flow_cmflowmixin_run_cm_flow_job           --calls--> lane_dispatch_handle
#   main                                                  --calls--> lane_router_warm_up
```
