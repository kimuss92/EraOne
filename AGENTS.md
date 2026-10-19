# Project Context Instructions

## Shared work log (read first, write last)

Two agents work on this repo — Claude Code and ChatGPT (CodeMode and other work).
To stay in sync:

1. **Before starting**, read the top entries of `handoff.md` (newest first) and the
   "In progress" table of `TODO.md`. They record what the other agent changed,
   what is verified, what is still owed, and the rules to respect.
2. **After each fix or improvement**, add an entry at the top of `handoff.md`
   (under "Phase 0"): date · who · title, then what was wrong, what changed (files),
   how it was verified, and what is NOT yet proven. Add owed live tests to `TODO.md`.

## Knowledge Graph

This project has a persistent knowledge graph at `graphify-out/` with 3699 nodes and 7944 edges.

### How to Use

**For codebase questions:**
```bash
graphify query "How does authentication work?"
```
This queries the knowledge graph directly instead of re-reading files.

**For architecture overview:**
Read `graphify-out/GRAPH_REPORT.md` for a human-readable summary of the codebase structure.

**For specific components:**
Use `graphify explain "ComponentName"` to get details about a specific component.

### What NOT to Do

- Do NOT re-read files that are already in the knowledge graph
- Do NOT scan directories when the graph already has the structure
- Do NOT ask about codebase structure when `graphify query` can answer it

### Session Start

When starting a new session, the graphify plugin will automatically:
1. Load the knowledge graph context
2. Show you the top modules and key nodes
3. Remind you to use `graphify query` for questions

### Knowledge Graph Location

All knowledge is stored in `graphify-out/`:
- `graph.json` - Full knowledge graph (3699 nodes)
- `GRAPH_REPORT.md` - Human-readable report
- `.context_summary.json` - Quick context summary
- `.read_cache.json` - Cached file reads

## Skills

### Netflix Controller

Location: `skills/netflix-controller/SKILL.md`

Provides comprehensive Netflix automation knowledge:
- Keyboard shortcuts (play/pause, seek, subtitles, fullscreen)
- Player DOM structure and selectors
- URL patterns and state detection
- Best practices for Netflix automation
- Common automation patterns (search and play, skip intro, toggle subtitles)

Use this skill when working with Netflix control in `actions/netflix_control.py` or `actions/browser_control.py`.
