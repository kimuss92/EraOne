"""Keep the era-ads-manager skill in step with Meta's changes.

Owner, 2026-11-20: the skill "must always follow the changes Facebook made on Meta Ads:
the new rules, the conditions". Meta changes objectives, buttons, destinations, policies
and conditions often; a stale rule in the skill costs real ad spend.

WHAT THIS DOES
  A weekly read-only run searches Meta's own help center (through the facebook-ads MCP's
  `ads_get_help_article`) for a pinned list of topics, extracts the rule statements, and
  compares them with the last digest. A real difference writes a dated note to the vault
  Inbox and raises a warning in every ads brief until a human has reviewed the sources,
  updated the skill and run `python -m core.meta_spec_watch ack`.

WHAT IT DOES NOT DO
  It never edits the skill, a campaign or anything that spends. It cannot see every Meta
  announcement, the tool flattens tables, and the statements are extracted by a model, so
  a flag means "re-read the source", not "the rule changed". A check that could not run is
  reported as such: silence is never "up to date".

Degrade, never raise: a failure here costs a clear status line, never an ads job.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
DIGEST_PATH = RUNTIME / "meta_specs_digest.json"
STATUS_PATH = RUNTIME / "meta_specs_status.json"

# A digest older than this (or never made) is itself a warning in the ads brief.
CHECK_MAX_AGE_DAYS = 10
# Two statement sets this similar are "the same topic" (absorbs rewording by the extractor).
SIMILARITY_FLOOR = 0.80

TOPICS: tuple[dict, ...] = (
    {
        "id": "cta_by_objective",
        "query": "Available call-to-action buttons in Ads Manager",
        "focus": "which buttons exist for each objective (Awareness, Traffic, Engagement, Leads, "
        "App promotion, Sales) and the footnotes that restrict a button to one destination",
    },
    {
        "id": "conversion_locations",
        "query": "Available conversion locations and events by objective in Ads Manager",
        "focus": "which conversion locations (website, app, messaging apps, WhatsApp, calls, "
        "instant forms) each objective offers",
    },
    {
        "id": "messaging_ads",
        "query": "ads that click to Messenger Instagram Direct WhatsApp message template requirements",
        "focus": "rules and requirements for click-to-message ads: templates, welcome messages, "
        "destinations, WhatsApp Business requirements",
    },
    {
        "id": "ads_policy_changes",
        "query": "advertising policies update changes Meta ads",
        "focus": "recent changes or new conditions in Meta's advertising policies",
    },
    {
        "id": "creative_specs",
        "query": "ad specifications aspect ratios Reels Stories Feed requirements",
        "focus": "aspect ratios, resolutions, text limits, safe zones, video lengths",
    },
    {
        "id": "advantage_plus",
        "query": "Advantage+ campaigns audience placements budget requirements",
        "focus": "how a campaign qualifies as Advantage+ and what each setting changes",
    },
)


# ── pure helpers (tested without a network) ──────────────────────────────────


def _norm(statement: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%+]", " ", str(statement).lower())).strip()


def digest_prompt() -> str:
    topics = "\n".join(
        f'- id "{t["id"]}": search `{t["query"]}`; extract {t["focus"]}.' for t in TOPICS
    )
    return (
        "READ-ONLY WATCH. Use ONLY the tool mcp__facebook-ads__ads_get_help_article (it takes a "
        "search_query). For each topic below, search Meta's help center, read the best matching "
        "article text, and extract at most 25 short, factual rule statements COPIED or closely "
        "paraphrased from it (one fact per statement, no opinions, no table layout guesses). "
        "Tables arrive flattened: when a column cannot be read, say 'unreadable', do not guess. "
        "Never create or change anything.\n\nTOPICS:\n"
        + topics
        + "\n\nReply with ONE JSON object and nothing else: "
        '{"<topic id>": {"title": "<article title>", "rules": ["<statement>", ...]}, ...}. '
        'Use {"title": "", "rules": []} for a topic you could not retrieve.'
    )


def parse_digest(text: str) -> dict:
    """The first JSON object in the model's reply, reduced to {topic: {title, rules}}."""
    raw = str(text or "")
    start = raw.find("{")
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return {}
    out = {}
    wanted = {t["id"] for t in TOPICS}
    for tid, body in (data or {}).items():
        if tid in wanted and isinstance(body, dict):
            rules = [str(r).strip() for r in (body.get("rules") or []) if str(r).strip()]
            out[tid] = {"title": str(body.get("title") or "")[:200], "rules": rules[:25]}
    return out


def diff_digests(old: dict, new: dict) -> list[dict]:
    """Topics whose rule statements changed, with what was added and removed.

    A topic with no rules in either digest (not retrieved) is never reported as a change:
    missing evidence is not a change in Meta's rules."""
    changes = []
    for tid in sorted(set(old) | set(new)):
        a = {_norm(r): r for r in (old.get(tid) or {}).get("rules", [])}
        b = {_norm(r): r for r in (new.get(tid) or {}).get("rules", [])}
        if not a or not b:
            continue
        inter, union = set(a) & set(b), set(a) | set(b)
        similarity = len(inter) / len(union) if union else 1.0
        if similarity < SIMILARITY_FLOOR:
            changes.append(
                {
                    "topic": tid,
                    "similarity": round(similarity, 2),
                    "added": [b[k] for k in b if k not in a][:8],
                    "removed": [a[k] for k in a if k not in b][:8],
                }
            )
    return changes


# ── state ─────────────────────────────────────────────────────────────────────


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def status() -> dict:
    return _read(STATUS_PATH)


def _runner_claude(prompt: str) -> str:
    """Run the watch prompt headless, read-only, on Claude (OmniRoute) when it is up."""
    import era_dispatch as ed
    from core.llm import brains
    from core.win_subprocess import NO_WINDOW

    cfg = ed.load_brands()
    config_dir = (cfg.get("defaults") or {}).get("ads_claude_config_dir") or ed._claude_accounts(cfg)[0]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    state = brains.snapshot().get("claude")
    env["ANTHROPIC_MODEL"] = brains.CLAUDE_MODEL if state and state.available else ed._GATEWAY_COMBO
    proc = subprocess.run(
        [ed.CLAUDE_BIN, "-p", "--output-format", "json", "--allowedTools",
         "mcp__facebook-ads__ads_get_help_article", "--max-turns", "20"],
        input=prompt, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, cwd=tempfile.gettempdir(),
        creationflags=NO_WINDOW,
    )
    try:
        return str(json.loads(proc.stdout).get("result") or "")
    except Exception:  # noqa: BLE001
        return proc.stdout or ""


def _write_inbox_note(changes: list[dict], day: str) -> str:
    try:
        from core import cm_vault

        inbox = Path(cm_vault.VAULT_DIR) / "00 - Inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"Meta_Specs_Change_{day}.md"
        lines = [
            "---", "status: active", "project: meta", "type: log", "era_brief: false", "---", "",
            f"# Meta may have changed ({day})", "",
            "The weekly watch found different rule statements in Meta's help center. This is a "
            "flag to re-read the sources, not proof that a rule changed. Review, update "
            "`.claude/skills/era-ads-manager/SKILL.md`, then run `python -m core.meta_spec_watch ack`.", "",
        ]
        for c in changes:
            lines += [f"## {c['topic']} (similarity {c['similarity']})", ""]
            lines += [f"- ADDED: {r}" for r in c["added"]] + [f"- REMOVED: {r}" for r in c["removed"]] + [""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - the note is a convenience, the status file is the record
        return ""


def check(runner: Callable[[str], str] | None = None, today: Any = None) -> dict:
    """Run the watch once. Never raises. The first run only sets the baseline."""
    day = (today or _dt.date.today()).isoformat()
    try:
        text = (runner or _runner_claude)(digest_prompt())
        new = parse_digest(text)
        retrieved = [t for t, b in new.items() if b["rules"]]
        if not retrieved:
            st = {"checked_at": day, "ok": False, "reason": "no topic could be retrieved",
                  "changed": status().get("changed", []), "acknowledged": status().get("acknowledged", True)}
            _write(STATUS_PATH, st)
            return st
        previous = _read(DIGEST_PATH)
        old = previous.get("digest") or {}
        baseline = not old
        changes = [] if baseline else diff_digests(old, new)
        prior = status()
        note = _write_inbox_note(changes, day) if changes else ""
        st = {
            "checked_at": day,
            "ok": True,
            "baseline": baseline,
            "retrieved": retrieved,
            "changed": [c["topic"] for c in changes] or (prior.get("changed", []) if not prior.get("acknowledged", True) else []),
            "acknowledged": not changes and prior.get("acknowledged", True),
            "note": note or prior.get("note", ""),
        }
        _write(STATUS_PATH, st)
        # the digest on disk is only replaced when there is no open change, so the NEW
        # state is not silently accepted before a human has reviewed it
        if baseline or not st["changed"]:
            _write(DIGEST_PATH, {"digest": new, "saved_at": day})
        else:
            _write(DIGEST_PATH.with_name("meta_specs_digest_pending.json"), {"digest": new, "saved_at": day})
        return st
    except Exception as exc:  # noqa: BLE001
        st = {"checked_at": day, "ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200],
              "changed": status().get("changed", []), "acknowledged": status().get("acknowledged", True)}
        try:
            _write(STATUS_PATH, st)
        except Exception:  # noqa: BLE001
            pass
        return st


def ack(today: Any = None) -> dict:
    """The review is done: accept the pending digest as the new baseline and clear the flag."""
    day = (today or _dt.date.today()).isoformat()
    pending = DIGEST_PATH.with_name("meta_specs_digest_pending.json")
    data = _read(pending)
    if data.get("digest"):
        _write(DIGEST_PATH, data)
        try:
            pending.unlink()
        except OSError:
            pass
    st = {**status(), "changed": [], "acknowledged": True, "acknowledged_at": day}
    _write(STATUS_PATH, st)
    return st


def needs_catchup(today: Any = None, max_age_days: int = 7) -> bool:
    """True when no good check exists from the last `max_age_days` days. Era is often not
    running at the scheduled hour (owner 2026-11-20), so the scheduler asks this at startup
    and runs a catch-up check instead of silently missing a week."""
    st = status()
    now = today or _dt.date.today()
    try:
        return (not st.get("ok")) or (now - _dt.date.fromisoformat(st["checked_at"])).days > max_age_days
    except (KeyError, ValueError):
        return True


def watch_note(today: Any = None) -> str:
    """The brief block for the ads lane: an open change, a failed check, or a stale watch."""
    st = status()
    now = today or _dt.date.today()
    if st.get("changed") and not st.get("acknowledged", True):
        return (
            "=== META MAY HAVE CHANGED ITS RULES ===\n"
            f"The weekly watch flagged: {', '.join(st['changed'])} (see {st.get('note') or 'the vault Inbox'}). "
            "Re-read Meta's help articles for those topics with ads_get_help_article BEFORE building, "
            "follow the safer reading where they differ from the skill, and say so in your summary.\n\n"
        )
    try:
        age = (now - _dt.date.fromisoformat(st["checked_at"])).days if st.get("checked_at") else None
    except ValueError:
        age = None
    if age is None:
        return (
            "=== META RULES WATCH HAS NEVER RUN ===\n"
            "Nothing tracks Meta's changes yet: verify the objective, button and policy rows against "
            "Meta's live help articles (ads_get_help_article) before building.\n\n"
        )
    if not st.get("ok") or age > CHECK_MAX_AGE_DAYS:
        why = st.get("reason") or f"last successful check {age} days ago"
        return (
            "=== META RULES WATCH IS NOT CURRENT ===\n"
            f"({why}). Verify the objective, button and policy rows against Meta's live help "
            "articles (ads_get_help_article) before building.\n\n"
        )
    return ""


if __name__ == "__main__":  # python -m core.meta_spec_watch [check|status|ack]
    import sys

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    out = {"check": check, "ack": ack}.get(cmd, status)()
    print(json.dumps(out, ensure_ascii=False, indent=1))
