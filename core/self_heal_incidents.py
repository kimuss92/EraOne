"""core/self_heal_incidents.py — turn era.log into fixable incidents.

An INCIDENT is a failure that Python itself located: a crash record whose
deepest frame is inside Era's own code. That is the only evidence the healer
acts on. Model-written prose ("the directory lacks permissions", "the user is
frustrated") is never an input here — the 2026-09-21 and 2026-09-25 auto
reports showed that narrative drifts from the evidence, and a self-edit built
on a wrong story is worse than no edit.

Frames come ONLY from the structured `frames` field core/logger.log_crash
builds from the traceback object. The `traceback` TEXT ends with str(exc),
which can quote a web page — including lines shaped like a frame — so parsing
that text would let an attacker choose which file gets patched (security
review, 2026-09-28). Records without `frames` are simply not incidents.

Everything below is plain parsing; no model is called and nothing is written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "logs" / "era.log"
_TAIL_BYTES = 8_000_000  # era.log rotates at 5 MB; this always covers the live file
CAUGHT_EVENT = "tool.execution.caught_exception"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_<>.]{0,80}$")

# Failures whose cause lives OUTSIDE the code: the network, a provider's quota,
# credentials, a device that vanished. Patching Era's code cannot fix these and
# "fixing" them is how retry storms and swallowed errors get written.
EXTERNAL_ERROR_TYPES = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "ClientConnectorError",
        "ServerDisconnectedError",
        "gaierror",
        "SSLError",
        "PortAudioError",
        "ResourceExhausted",
        "ServiceUnavailable",
        "KeyboardInterrupt",
        "CancelledError",
        "SystemExit",
    }
)
_EXTERNAL_TEXT = re.compile(
    r"resource_exhausted|quota|rate.?limit|\b429\b|\b401\b|\b403\b|\b503\b|"
    r"unauthori[sz]ed|api key|getaddrinfo|network is unreachable|timed out|"
    r"keepalive ping|no close frame|interpreter shutdown",
    re.IGNORECASE,
)


@dataclass
class Incident:
    fingerprint: str  # error_type|rel|func — line numbers drift, functions don't
    error_type: str
    rel: str
    line: int
    func: str
    evidence: str  # the exception message: UNTRUSTED (may quote web pages)
    frames: list[tuple[str, int, str]] = field(default_factory=list)  # code-derived
    count: int = 0
    uncaught: int = 0  # records of exceptions that ESCAPED (log_crash)
    caught_calls: set[int] = field(default_factory=set)  # distinct calls, caught records
    first_seen: str = ""
    last_seen: str = ""
    sources: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("fingerprint", "error_type", "rel", "line", "func")}
        d.update(count=self.count, first_seen=self.first_seen, last_seen=self.last_seen)
        d["sources"] = sorted(self.sources)
        return d


def _rel_in_repo(path: str, root: Path) -> str | None:
    low = path.lower()
    if "site-packages" in low or not low.startswith(str(root).lower()):
        return None
    try:
        rel = Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None
    if rel.startswith(("runtime/", ".")) or "/." in rel or "worktrees/" in rel:
        return None  # backups, rejected copies, .claude worktrees
    return rel


def repo_frames(frames, root: Path | None = None) -> list[tuple[str, int, str]]:
    """The structured frames that are inside Era's own code, outermost first."""
    root = root or REPO_ROOT
    out = []
    for item in frames or []:
        try:
            path, line, func = str(item[0]), int(item[1]), str(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        rel = _rel_in_repo(path, root)
        if rel and _IDENT.match(func):
            out.append((rel, line, func))
    return out


def repo_frame(frames, root: Path | None = None) -> tuple[str, int, str] | None:
    """Deepest frame inside Era's own code. Third-party frames say where it blew
    up, the deepest REPO frame says what Era should change."""
    found = repo_frames(frames, root)
    return found[-1] if found else None


def is_external(error_type: str, evidence: str) -> bool:
    if error_type in EXTERNAL_ERROR_TYPES:
        return True
    return bool(_EXTERNAL_TEXT.search(evidence))


def _read_records(log_file: Path) -> list[dict]:
    try:
        size = log_file.stat().st_size
        with log_file.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop the partial first line
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"frames"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("frames"), list):
            out.append(rec)
    return out


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def scan(
    since: datetime | None = None,
    hours: float = 24,
    log_file: Path | None = None,
    root: Path | None = None,
) -> list[Incident]:
    """Code-located incidents in the window, most frequent first. External,
    test-run and benign (gate-held, shutdown-race) records are skipped."""
    from actions.self_diagnosis import _classify

    root = root or REPO_ROOT  # resolved per call: a default binds at import
    cutoff = since or (datetime.now(UTC) - timedelta(hours=hours))
    found: dict[str, Incident] = {}
    for rec in _read_records(log_file or LOG_FILE):
        ts = _parse_ts(rec.get("ts", ""))
        if ts is None or ts < cutoff or rec.get("test_run") is True:
            continue
        event = str(rec.get("event", ""))
        # Event + message only: file paths are not evidence of benign-ness, and
        # _classify's test-artifact rule would match any path containing "pytest".
        blob = f"{event} {rec.get('error') or ''}"
        if _classify(event, str(rec.get("level", "")), rec, blob) is not None:
            continue
        frames = repo_frames(rec.get("frames"), root)
        if not frames:
            continue
        error_type = str(rec.get("error_type") or "Error")
        if not _IDENT.match(error_type):
            continue
        evidence = str(rec.get("error") or "")[:500]
        if is_external(error_type, evidence):
            continue
        rel, line, func = frames[-1]
        fp = f"{error_type}|{rel}|{func}"
        inc = found.get(fp)
        if inc is None:
            inc = found[fp] = Incident(fp, error_type, rel, line, func, evidence)
            inc.first_seen = rec["ts"]
        inc.count += 1
        if event == CAUGHT_EVENT:
            call = rec.get("call_id")
            inc.caught_calls.add(call if isinstance(call, int) else -inc.count)
        else:
            inc.uncaught += 1
        inc.last_seen, inc.line, inc.evidence, inc.frames = rec["ts"], line, evidence, frames
        inc.sources.add(str(rec.get("tool") or rec.get("source") or event)[:48])
    return sorted(found.values(), key=lambda i: (-i.count, i.fingerprint))


def occurrences_since(fingerprint: str, since: datetime, log_file: Path | None = None) -> int:
    return sum(
        i.count for i in scan(since=since, log_file=log_file) if i.fingerprint == fingerprint
    )


def new_faults_in(rel: str, since: datetime, known: set[str], log_file: Path | None = None):
    """Fingerprints in `rel` seen after `since` that were not known before —
    a regression the patch introduced rather than the one it targeted. A lone
    caught-exception record is not enough: rolling a good fix back must take
    an escaped crash or the same caught fault in two separate calls."""
    return [
        i.fingerprint
        for i in scan(since=since, log_file=log_file)
        if i.rel == rel and i.fingerprint not in known and (i.uncaught or len(i.caught_calls) >= 2)
    ]
