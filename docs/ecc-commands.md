# ECC commands (vendored)

94 slash commands from the ECC plugin, available here as `/ecc:<name>` —
`/ecc:build-fix`, `/ecc:code-review`, `/ecc:checkpoint`, and so on.

| | |
|---|---|
| Source | https://github.com/affaan-m/ECC |
| Version | 2.2.2 |
| Commit | `c70874f` |
| Licence | MIT — Copyright (c) 2026 Affaan Mustafa |
| Vendored | 2026-10-01 |

## Why these are copied into the repo rather than installed as a plugin

A plugin installed locally with `/plugin` lives in your own `~/.claude/` on that
machine. Claude Code on the web starts from a fresh container that clones this
repository and nothing else, so a locally-installed plugin is simply absent
there — which is exactly why `/ecc:build-fix` did nothing on 2026-10-01.
Anything committed under `.claude/` travels with the repo and works in every
session, the same way `.claude/commands/github/` already does.

## What was deliberately NOT taken

ECC ships far more than these commands. The rest was left out on purpose, and
the reasons matter if you ever want to add it:

* **293 skills.** Every skill's name and description is loaded into context on
  every turn. This repo already carries ~50; adding 293 more would be a large,
  permanent context tax on a project whose whole discipline is careful gating.
  Take individual skills if you want them, not the set.
* **68 agents.** Same argument, and they overlap heavily with the agent roster
  already defined in `CLAUDE.md`.
* **5 hooks / `hooks.json` (33 KB).** Hooks are executable automation that runs
  on tool calls, and ECC's manifest defaults `hooks_enabled` to **true**. This
  repo already has its own safety-critical hooks — `guard_protected_paths.py`
  and `post_edit_format.py` — and third-party automation sitting in the same
  path is a decision to make deliberately, not a side effect of wanting one
  command.
* **MCP configs** (`.mcp.json`, `mcp-configs/`), which would add servers beside
  the ones this project already declares.

The commands are plain Markdown and were checked for references to ECC's own
scripts or hook paths before vendoring; there are none, so they are
self-contained instructions with nothing to execute.

## Updating

Re-clone the source at the tag you want and copy `commands/*.md` over this
directory, then update the version and commit above. Nothing else here is
generated, so a straight copy is the whole update.
