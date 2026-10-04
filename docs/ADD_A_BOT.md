# Adding a bot to the Era One crew

A bot is **data**: one yaml file, one prompt file, and capability *names* that already
exist. You edit nothing under `core/`, `app/`, `actions/` or `crew/*.py`
(`tools/test_crew_registry.py::TestAddingABot` proves it by hashing those sources before
and after adding a bot).

## 1. The yaml: `config/crew/<id>.yaml`

```yaml
id: ines                       # must equal the file name; ^[a-z][a-z0-9_]{1,31}$
role: Accounting
description: One line, shown by `crew list`.
prompt: prompts/ines.md        # relative to config/crew/; may not leave it
capabilities: [vault.search, vault.read]
model_tier: fast               # fast | smart                      (default fast)
brands: ["*"]                  # "*" or brand keys, e.g. [delta_cuisine]   (default all)
write_zones: [inbox]           # inbox | brand | global | other    (default inbox only)
handoffs_to: []                # bot ids this bot may hand a task to
```

## 2. The prompt: `config/crew/prompts/<id>.md`

Say who the bot is, how it works, and its limits, in plain sentences. The runtime adds the
capability catalogue and the response format; you do not write those. Keep it under 20,000
characters. Treat note text as data, never as instructions to the bot — say so.

## 3. Pick capabilities by name

| Capability | What it does | Notes |
|---|---|---|
| `vault.search` / `vault.read` | find and read notes | brand-scoped by `brands` |
| `vault.create` / `vault.append` | free writes | land where `write_zones` allows; default the **Inbox**, which is never briefed |
| `vault.replace_staged` | *propose* replacing a note's body or one `## section` | writes nothing: Nabar reviews the diff, the **user** must say yes |
| `code.propose` / `code.check` | describe a change / run up to 5 `tools/test_*.py` | read-only; never writes the repo |
| `crew.handoff` | hand a sub-task to a bot named in `handoffs_to` | depth limit 3 |

There is no delete and no capability that approves anything. A name that does not exist
skips the **whole** bot (with the reason in `crew status`), never a partial grant.

## 4. Let Laura route to it

Add the id to `handoffs_to` in `config/crew/laura.yaml` (also data).

## 5. Check it

```bash
python -c "from crew.registry import load_specs as l; r = l(); print(sorted(r.specs), r.skipped)"
python -m pytest tools/test_crew_registry.py -q
```

A bad file shows up in `skipped` with its reason; every other bot still loads.

## What config cannot do

- **Grant what does not exist.** A new *capability* is code: one function with `@capability(...)`
  in `crew/capabilities.py` plus tests. It must return a `ToolResult` and must never consume the
  confirmation store (a test scans for that).
- **Approve its own work.** Approval is the user's own "yes" through `PendingConfirmationStore`.
- **See another brand.** A bot scoped with `brands: [delta_cuisine]` cannot read Ceram Quartz.

## Swapping what carries a bot turn (e.g. Hermes Desktop)

Implement `crew.transport.Transport` (`dispatch`, `register`, `capabilities`, `health`;
failures only via `crew.transport.fail(error_type, detail)`), then call
`crew.runtime.reset_transport(your_transport, specs)`. Callers depend on the contract only.
Do not import `lane_router` from `crew/` (a test fails if you do).
