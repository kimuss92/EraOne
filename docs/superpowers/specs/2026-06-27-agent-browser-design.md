# ADR-002: Agent Browser Integration

## Status
Accepted

## Context
JARVIS currently uses `browser_control` (Playwright-based CDP connection) for simple browser actions — navigating to URLs, switching/closing tabs, scrolling. However, complex multi-step web tasks (searching, form filling, data extraction, multi-page research) require a more capable browser automation agent.

The [agent-browser](https://github.com/vercel-labs/agent-browser) CLI provides LLM-powered browser automation that can understand natural language instructions and execute multi-step workflows.

## Concerns
Connecting to the user's existing browser via CDP (`--auto-connect`) must not interfere with:
1. **Netflix fullscreen** — agent-browser operates in a dedicated new tab, never touches existing netflix/youtube tabs, and does not call `bring_to_front()`
2. **Website theming** — CDP connections are per-page scoped; agent-browser opens its own tabs without modifying browser-level settings like `Browser.setDownloadBehavior`

## Decision
Add `agent_browser` as a separate tool alongside (not replacing) `browser_control`.

### Tool Boundary
| Tool | Use Case |
|---|---|
| `browser_control` | Simple nav: open URL, back/forward, close tab, scroll |
| `agent_browser` | Complex multi-step: "search for the best laptop under $1000 and summarize specs" |

### CDP Safety Design
1. `--auto-connect` attaches to the existing browser; no new browser launched
2. agent-browser opens its own new tab for each task
3. No `bring_to_front()`, no fullscreen API calls, no download-behavior changes
4. All operations scoped to the agent-browser's tab

### Integration Points
| Layer | Change |
|---|---|
| `actions/agent_browser_use.py` | **NEW** — async subprocess call to `agent-browser CLI` |
| `main.py` — import | Add import at top |
| `main.py` — TOOL_DECLARATIONS | Add tool definition for LLM |
| `main.py` — dispatch | New `elif tool_name == "agent_browser"` |
| `core/action_registry.py` | Add canonical name mapping |
| `agent/executor.py` | Add dispatch for executor context |
| `core/tool_registry_validator.py` | Register in `_ACTION_CLASSIFIED_TOOLS` |

### Flow
```
User: "find the cheapest flight to Tokyo next week"
  → LLM calls agent_browser(query="find cheapest flight ...")
  → main.py dispatches to agent_browser_use.py
  → Subprocess: agent-browser run "..." --auto-connect
  → agent-browser opens new tab in existing Brave, completes task
  → Returns result text
```

## Consequences
- No changes to `browser_control.py`, `netflix_control.py`, `spotify_control.py`
- agent-browser CLI must be installed globally (`npm install -g agent-browser`)
- System prompt updated to guide LLM when to use which browser tool
