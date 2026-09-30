"""actions/protocol_zero.py — Protocol Zero, the prompt-synchronisation auditor.

Keeps core/prompt.txt (Era's system prompt) in step with what Era can actually
do. On 2026-09-16, 17 of Era's 45 tools — self_improve, vault_note, forget_memory,
desktop_control among them — were never mentioned anywhere in the prompt.

Where capabilities come from
  app/tool_declarations.py is the one authoritative list of what Era can do: it
  is exactly what the Live model is given. It is read from DISK with
  ast.literal_eval (it is pure data), so a running Era sees today's file, not the
  copy it imported at startup — and nothing in it is executed.

What gets written, and why it is not model-generated
  Every description comes from a declaration a developer wrote in code. Letting a
  model compose text that is injected into Era's system prompt would make the
  prompt writable by whatever that model was shown; deriving it from the
  declarations keeps the prompt's authority in the codebase.

The managed block
  All output lives in ONE block at the end of prompt.txt, between fixed markers,
  and is REGENERATED on each sync rather than appended to. Running Zero twice
  changes nothing; a removed tool drops out; hand-written sections are never
  touched. Coverage is measured against the hand-written text only, so the block
  documents exactly what the hand-written prompt does not.

Triggers: the user saying "Launch Protocol Zero" (checked against the user's own
words in tool_dispatch), or VS Code opening on this project (core/protocol_spies
VSCodeSpy). A drift CHECK runs continuously in the spies; a WRITE never does.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core import hive
from core.utils import log

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "core" / "prompt.txt"
DECL_PATH = REPO_ROOT / "app" / "tool_declarations.py"
BACKUP_DIR = REPO_ROOT / "runtime" / "protocol_zero" / "backups"
_KEEP_BACKUPS = 10

HEADER = "━━━ 16. CAPABILITY INDEX (maintained by Protocol Zero) ━━━"
BEGIN = "[PROTOCOL ZERO: BEGIN AUTO-SYNCED BLOCK — generated from app/tool_declarations.py]"
END = "[PROTOCOL ZERO: END AUTO-SYNCED BLOCK]"
_INTRO = (
    "Tools and actions below are available to you but not described elsewhere in this "
    "prompt. Each line is the tool's own declaration, condensed. Earlier sections win "
    "on any conflict."
)
_DESC_CHARS = 260
_MAX_BLOCK_CHARS = 9000

_RUN_LOCK = threading.Lock()


# ── reading ──────────────────────────────────────────────────────────────────
def _literal(tree: ast.Module, name: str):
    """The first module-level literal assigned to `name`, or None.

    Only literals: a later reassignment built from a comprehension is skipped
    rather than guessed at, because guessing is what would put a tool Era
    cannot call into her prompt.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
    return None


def load_declarations(path: Path = DECL_PATH) -> list[dict]:
    """The tools the live model is actually offered, read without importing.

    WHY THE GATE IS APPLIED HERE
      app/tool_declarations.py declares every tool, then withholds the ones
      whose config flag is off — that filtering happens at import, ~2,000
      lines below the literal. Reading only the literal reported 53 tools
      where the model was offered 52, and Protocol Zero wrote the withheld
      `spotify_bridge` into Era's system prompt as a capability she has.

      Reading the file rather than importing it is deliberate (see this
      module's docstring), so the fix is not to import: the gates are declared
      as data in RUNTIME_GATED_TOOLS, and the same flags are read here. A new
      gated tool is handled by that one data structure, not by a second edit
      that someone has to remember to make.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    decls = _literal(tree, "TOOL_DECLARATIONS")
    if decls is None:
        raise ValueError(f"TOOL_DECLARATIONS not found in {path}")

    gates = _literal(tree, "RUNTIME_GATED_TOOLS") or {}
    withheld = set()
    for name, where in gates.items():
        try:
            section, key = where
        except (TypeError, ValueError):
            continue
        if not _gate_is_open(str(section), str(key)):
            withheld.add(name)
    if withheld:
        decls = [d for d in decls if d.get("name") not in withheld]
    return decls


def _gate_is_open(section: str, key: str) -> bool:
    """Is this tool's config flag on? Unreadable config means closed.

    Fails closed on purpose: treating an unreadable flag as ON would document
    a tool Era may not have, which is the exact failure this gate prevents.
    """
    try:
        from core.utils import load_config

        return bool((load_config().get(section) or {}).get(key))
    except Exception:  # noqa: BLE001 - see docstring; any failure means closed
        return False


def action_tokens(decl: dict) -> list[str]:
    """Action names from an enum, or from an 'a | b | c' action description."""
    spec = (decl.get("parameters") or {}).get("properties", {}).get("action") or {}
    if spec.get("enum"):
        return [str(x) for x in spec["enum"]]
    text = str(spec.get("description") or "")
    if "|" not in text:
        return []
    tokens = []
    for seg in text.split("|"):
        m = re.match(r"\s*(?:[A-Za-z ]+:\s*)?([a-z][a-z0-9_]{1,40})\b", seg)
        if m and m.group(1) not in tokens:
            tokens.append(m.group(1))
    return tokens


def split_prompt(text: str) -> tuple[str, str | None]:
    """(hand-written text, managed block body or None)."""
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1 or end < start:
        return text, None
    hand = text[:start]
    if hand.rstrip().endswith(HEADER):
        hand = hand.rstrip()[: -len(HEADER)]
    hand = hand.rstrip() + "\n" + text[end + len(END) :].lstrip("\n")
    return hand.rstrip() + "\n", text[start + len(BEGIN) : end].strip("\n")


def _mentioned(word: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text) is not None


# UTF-8 read as cp1252 somewhere upstream; several declarations still carry it.
# Copying "â€”" into the system prompt would spread the damage, so repair it here.
_MOJIBAKE = {
    "\u00e2\u20ac\u201d": "\u2014",  # em dash
    "\u00e2\u20ac\u201c": "\u2013",  # en dash
    "\u00e2\u20ac\u2122": "'",
    "\u00e2\u20ac\u0153": '"',
    "\u00e2\u20ac\u009d": '"',
    "\u00e2\u2020\u2019": "\u2192",  # right arrow
}
_SENTENCE_END = re.compile(r"(?<!e\.g)(?<!i\.e)[.;](?=\s)")


def _condense(description: str) -> str:
    text = " ".join(str(description).split())
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    if len(text) <= _DESC_CHARS:
        return text
    cut = text[:_DESC_CHARS]
    stops = [m.end() for m in _SENTENCE_END.finditer(cut) if m.end() > 80]
    return (cut[: stops[-1]] if stops else cut.rstrip() + "…").strip()


# ── auditing ─────────────────────────────────────────────────────────────────
def entries_for(decls: list[dict], hand_text: str) -> dict[str, str]:
    """name -> one index line, only for what the hand-written prompt lacks."""
    out: dict[str, str] = {}
    for d in decls:
        name = d["name"]
        actions = action_tokens(d)
        required = (d.get("parameters") or {}).get("required") or []
        if not _mentioned(name, hand_text):
            line = f"- {name}: {_condense(d.get('description', ''))}"
            if actions:
                line += f" Actions: {' | '.join(actions)}."
            if required:
                line += f" Required: {', '.join(required)}."
        else:
            missing = [a for a in actions if not _mentioned(a, hand_text)]
            if not missing:
                continue
            line = f"- {name} — further actions: {' | '.join(missing)}."
        out[name] = line.replace(BEGIN, "").replace(END, "")
    return out


def parse_block(body: str | None) -> dict[str, str]:
    entries = {}
    for line in (body or "").splitlines():
        m = re.match(r"- ([a-z][a-z0-9_]*)[: ]", line)
        if m:
            entries[m.group(1)] = line
    return entries


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    written: bool = False
    backup: str = ""
    tools_total: int = 0
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    def summary(self) -> str:
        if self.error:
            return f"Protocol Zero could not sync the prompt: {self.error}"
        if not self.changed:
            return (
                f"Protocol Zero: prompt.txt is in sync with all {self.tools_total} of my tools. "
                "Nothing changed."
            )
        parts = []
        if self.added:
            parts.append(f"added {len(self.added)} ({', '.join(self.added)})")
        if self.modified:
            parts.append(f"updated {len(self.modified)} ({', '.join(self.modified)})")
        if self.removed:
            parts.append(f"removed {len(self.removed)} ({', '.join(self.removed)})")
        verb = "synced" if self.written else "would sync"
        text = f"Protocol Zero {verb} prompt.txt: {'; '.join(parts)}."
        if self.truncated:
            text += f" Size cap left out: {', '.join(self.truncated)}."
        if self.written:
            text += " It takes effect on my next session."
        return text


def _render_block(entries: dict[str, str]) -> tuple[str, list[str]]:
    lines, used, dropped = [], 0, []
    for name in sorted(entries):
        line = entries[name]
        if used + len(line) > _MAX_BLOCK_CHARS:
            dropped.append(name)
            continue
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), dropped


def sync(
    prompt_path: Path | None = None, decl_path: Path | None = None, apply: bool = True
) -> SyncReport:
    # Resolved at CALL time. As `= PROMPT_PATH` defaults they were bound at import,
    # so a test that patched PROMPT_PATH still wrote the real prompt (2026-09-16).
    prompt_path = prompt_path or PROMPT_PATH
    decl_path = decl_path or DECL_PATH
    report = SyncReport()
    if apply and _is_live_prompt_under_pytest(prompt_path):
        report.error = "refusing to write the real core/prompt.txt from inside pytest"
        return report
    try:
        decls = load_declarations(decl_path)
        raw = prompt_path.read_bytes()
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except (OSError, ValueError, SyntaxError) as e:
        report.error = f"{type(e).__name__}: {e}"
        return report
    report.tools_total = len(decls)

    hand, old_body = split_prompt(text)
    old = parse_block(old_body)
    new = entries_for(decls, hand)
    body, report.truncated = _render_block(new)
    kept = parse_block(body)

    report.added = sorted(set(kept) - set(old))
    report.removed = sorted(set(old) - set(kept))
    report.modified = sorted(n for n in set(kept) & set(old) if kept[n] != old[n])
    if not (apply and report.changed):
        return report

    content = hand if not kept else f"{hand}\n{HEADER}\n{BEGIN}\n{_INTRO}\n{body}\n{END}\n"
    report.backup = _backup(prompt_path)
    fd, tmp = tempfile.mkstemp(prefix="prompt", suffix=".tmp", dir=str(prompt_path.parent))
    # Keep the file's own line endings: prompt.txt is CRLF, and rewriting every
    # line would make one sync look like a whole-file change in any diff tool.
    with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(content)
    os.replace(tmp, prompt_path)
    report.written = True
    return report


def _backup(prompt_path: Path) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"prompt_{datetime.now():%Y%m%d_%H%M%S_%f}.txt"
    shutil.copy2(prompt_path, dest)
    for old in sorted(BACKUP_DIR.glob("prompt_*.txt"))[:-_KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(dest)


# ── the protocol ─────────────────────────────────────────────────────────────
DRIFT_SIGNATURE = "protocol_zero:prompt_drift"


def _is_live_prompt_under_pytest(prompt_path: Path) -> bool:
    """Last line of defence: the suite must never rewrite Era's real system prompt,
    whatever a test forgets to patch. Same idea as core.logger's pytest tag."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return prompt_path.resolve() == (REPO_ROOT / "core" / "prompt.txt").resolve()
    except OSError:
        return True


def check_drift(prompt_path: Path | None = None, decl_path: Path | None = None) -> SyncReport:
    """Read-only audit. Posts or resolves the drift insight in the hive."""
    report = sync(prompt_path, decl_path, apply=False)
    if report.error:
        return report
    if report.changed:
        names = report.added + report.modified + report.removed
        hive.post_insight(
            "protocol_zero",
            DRIFT_SIGNATURE,
            severity="low",
            category="config",
            title=f"prompt.txt out of sync with {len(names)} tool(s)",
            evidence=", ".join(names)[:600],
            location="core/prompt.txt",
        )
    else:
        hive.resolve_insight(DRIFT_SIGNATURE, by="protocol_zero", note="in sync")
    return report


def execute_protocol_zero(
    parameters: dict | None = None, player=None, speak=None, trigger: str = "voice"
) -> str:
    """Entry point (sync; run it in a background thread)."""
    if not _RUN_LOCK.acquire(blocking=False):
        return "Protocol Zero is already running."
    try:
        hive.set_status("protocol_zero", "auditing", f"triggered by {trigger}")
        report = sync()
        summary = report.summary()
        if report.error:
            hive.set_status("protocol_zero", "failed", report.error)
            return summary
        if report.written or not report.changed:
            hive.resolve_insight(DRIFT_SIGNATURE, by="protocol_zero", note="synced")

        p1 = [m for m in hive.inbox("protocol_zero") if m.get("kind") == "cycle_report"]
        if p1 and isinstance(p1[-1].get("payload"), dict):
            counts = p1[-1]["payload"].get("counts") or {}
            open_hi = int(counts.get("critical", 0)) + int(counts.get("high", 0))
            summary += f" Protocol One's last cycle ({p1[-1]['at']}) left {open_hi} critical/high issue(s)."

        hive.set_status("protocol_zero", "idle", summary, trigger=trigger)
        hive.send_message(
            "protocol_zero",
            "*",
            "prompt_sync",
            {
                "added": report.added,
                "modified": report.modified,
                "removed": report.removed,
                "backup": report.backup,
                "trigger": trigger,
            },
        )
        if report.written:
            _log_to_vault(report, trigger)
        log("PROTOCOL0", summary, player)
        return summary
    except Exception as e:
        hive.set_status("protocol_zero", "failed", f"{type(e).__name__}: {e}")
        return f"Protocol Zero stopped with an error: {type(e).__name__}: {e}"
    finally:
        _RUN_LOCK.release()


def _log_to_vault(report: SyncReport, trigger: str) -> None:
    try:
        from core import daily_note

        daily_note.append_session(
            "Protocol Zero prompt sync",
            done=[report.summary(), f"Trigger: {trigger}", f"Backup: {report.backup}"],
        )
    except Exception as e:
        log("PROTOCOL0", f"vault day-log skipped: {e}")


def protocol_zero_status() -> str:
    return hive.briefing()
