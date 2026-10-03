"""actions/self_diagnosis.py — Era deep-diagnoses her own code and proposes fixes.

Voice/text: "run a diagnosis on yourself", "why did that error happen", "check your
own code". Read-only — it NEVER modifies code; it produces a report. Protocol One
(actions/protocol_one.py) reuses these phases before it fixes anything.

Four bounded phases (any phase failing never kills the run):
  1. SYNTAX  — in-memory compile() sweep over Era's own source tree.
  2. DOCTOR  — tools/era_doctor.py healthcheck in a subprocess.
  3. LOGS    — logs/era.log over the last 24h, deduplicated by signature, and each
               signature carries its REAL evidence: the error text and the deepest
               frame inside Era's own code.
  4. ANALYZE — the model explains the evidence. It does NOT decide the verdict.

WHY THE ANALYSIS IS FENCED IN (2026-09-16)
  The previous version handed the model bare signatures ("2x engine.loop.uncaught
  [ERROR]") with no message and no location. The model filled the gap by
  inventing: it told the user to fix `tools/netflix_control.py` (no such file),
  and declared Era "Unstable, critical" from two log lines. So now:
    * the verdict is computed here from the evidence, never by the model;
    * the model must answer in JSON, one entry per signature it was given;
    * an entry citing a signature we did not give it is dropped;
    * a file path it cites that does not exist in the repo is replaced by the
      location we actually extracted, and flagged as corrected in the report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.utils import log
from core.win_subprocess import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = _REPO_ROOT / "runtime" / "diagnosis"

# Era's own source: top-level scripts + these packages. Everything else (node
# modules, caches, third-party, media, backups) is noise and would blow the budget.
_SOURCE_DIRS = ("actions", "agent", "app", "core", "memory", "utils", "tools", "windows")
_SKIP_PARTS = {"__pycache__", "node_modules", ".git", "dist"}
_DOCTOR_TIMEOUT_S = 120

# Counts are only meaningful over a defined period — see git-less history in the
# report dir: a fixed line-tail once turned 557 real events into "46 occurrences".
_LOG_WINDOW_HOURS = 24
_ERROR_LINE = re.compile(r"ERROR|Traceback|CRITICAL|\[QUOTA\]|Exception:", re.IGNORECASE)

# A test fixture corrupting a throwaway file is proof the guard works, not a fault.
# core.logger.safe_log_path tags out-of-repo paths `<external>/…`; match the tag.
_EXTERNAL_PATH = re.compile(r"<external>/", re.IGNORECASE)
_TEST_ARTIFACT = re.compile(r"\btest[_.][a-z_]*\.jsonl?\b|pytest", re.IGNORECASE)

# The network going away, as Windows and asyncio spell it. Mirrors
# ReconnectMixin._is_network_blip (which now tags NEW lines at the source); this
# copy exists for lines written before a marker was added there — tagging is not
# retroactive. "WinError 1231 The network location cannot be reached" was being
# reported as a real engine.reconnect_error until 2026-09-16.
NETWORK_MARKERS = (
    "1006",
    "abnormal closure",
    "connection reset",
    "winerror 64",
    "winerror 1231",
    "winerror 1232",
    "winerror 10051",
    "winerror 10065",
    "network location cannot be reached",
    "network name is no longer available",
    "getaddrinfo failed",
    "temporary failure in name resolution",
)

# The interpreter tearing down, spelled as it reaches era.log. Mirrors
# ReconnectMixin._is_interpreter_shutdown (the live source of truth, which
# matches on the exception object) — this copy matches on the logged text.
# `RuntimeError: cannot schedule new futures after interpreter shutdown` is
# raised ONLY while Python is finalizing: the reconnect loop in a daemon thread
# racing process exit. It is a lifecycle artifact of a closed window whose
# process outlived it, never a live memory, threading or engine defect — see
# the shutdown-zombie note. A storm of these in the 24h window is a stale
# reading of an already-guarded exit, not something to fix or flag.
_SHUTDOWN_RACE_MARKER = "interpreter shutdown"

_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')

# The provider failing on its own side, matched on LOGGED TEXT — self-diagnosis
# reads era.log, so it cannot use the exception-object predicate the Live session
# uses (ReconnectMixin._is_upstream_service_error). The marker list is IMPORTED
# rather than copied: NETWORK_MARKERS above is a copy, and keeping two in step is
# how "WinError 1231" stayed mis-reported for weeks.
_UPSTREAM_BENIGN_CLASS = "upstream_service_error"

# How many recovered upstream errors in the 24h window stop being "expected" and
# become worth saying out loud. A FIRST CUT, not a measurement: the only data
# point is the 18 seen on 2026-09-30, all of which Era recovered from. It is a
# named constant so it can be retuned once there is a baseline for a normal day.
_UPSTREAM_RATE_NOTE = 12

_SUPPRESSED_LINE = re.compile(r"^\s*(\d+)x\s+(\S+)")


def _is_upstream_text(text: str) -> bool:
    """True if this logged error text is the provider's own 5xx.

    Shares `core.ai_provider`'s marker list so there is ONE definition of "the
    service failed on its side", and inherits its quota veto — a 429 must not be
    dismissed as a transient blip, because quota needs a key rotation or a long
    cooldown, not a reconnect.
    """
    from core.ai_provider import QUOTA_MARKERS, TRANSIENT_SERVER_MARKERS

    low = text.lower()
    if any(m in low for m in QUOTA_MARKERS):
        return False
    return any(m in low for m in TRANSIENT_SERVER_MARKERS)


def suppressed_count(suppressed: list[str] | None, klass: str) -> int:
    """Total occurrences of one benign class in `phase_logs`' suppressed list.

    Parses the `"{n}x  {cls} — why"` lines that `phase_logs` builds a few
    functions below. Producer and consumer are both in this file, and
    `test_the_rate_signal_reads_what_phase_logs_writes` pins them together so the
    format cannot drift apart silently.
    """
    total = 0
    for line in suppressed or []:
        m = _SUPPRESSED_LINE.match(str(line))
        if m and m.group(2) == klass:
            total += int(m.group(1))
    return total


def _is_test_origin(rec: dict, raw: str) -> bool:
    path = str(rec.get("path", ""))
    if _EXTERNAL_PATH.search(path):
        return True
    # Legacy lines: every file Era owns is logged WITH its package dir, so a bare
    # basename can only have been an out-of-repo temp copy.
    if path and "/" not in path and "\\" not in path:
        return True
    if _TEST_ARTIFACT.search(path):
        return True
    return bool(_TEST_ARTIFACT.search(raw))


def _classify(event: str, level: str, rec: dict, raw: str) -> tuple[str, str] | None:
    """Return (benign_class, why) if this log line is expected noise, else None.

    Every rule states WHY the event is not a defect. The default is to report.
    """
    blob = raw.lower()

    # Written under pytest (core.logger tags test_run). Trust the tag only — the
    # logger name cannot be used, era_one.executor serves real tool calls too.
    if rec.get("test_run") is True:
        return (
            "test_suite_run",
            "Emitted while pytest was driving the process — an exercised failure path, not a live fault.",
        )

    # A defensive teardown, not a defect: the reconnect loop racing interpreter
    # shutdown. Checked before the engine rules below because the RuntimeError
    # text is definitive on its own, whatever event label carried it — this is
    # the exact false positive Protocol One kept raising as a "memory" fault.
    if _SHUTDOWN_RACE_MARKER in blob:
        return (
            "shutdown_race",
            "'cannot schedule new futures after interpreter shutdown' — the reconnect "
            "loop racing process exit while Python finalizes. A lifecycle artifact of a "
            "closed window, already guarded at the source; not a memory, lock or engine "
            "fault, and nothing to fix.",
        )

    if event in ("engine.session.rotated", "engine.reconnect_error", "engine.crash"):
        if "1008" in blob or "policy violation" in blob or "operation was aborted" in blob:
            return (
                "gemini_session_rotation",
                "Expected ~2.5min Live-session rotation; reconnect+resumption already handles it.",
            )
        # Only when EVERY leaf is a network error: an ExceptionGroup that also
        # holds a PortAudioError is a real audio fault and must stay visible.
        leaves = rec.get("leaf_errors") or [rec.get("error", "")]
        if leaves and all(any(m in str(x).lower() for m in NETWORK_MARKERS) for x in leaves):
            return (
                "network_blip",
                "Connection dropped (Wi-Fi/DNS); the reconnect loop restores the session automatically.",
            )
        # The same all-leaves rule for the provider's own 5xx. These now log as
        # engine.upstream.error at the source, but tagging is not retroactive —
        # this is what reads the engine.crash lines already in era.log, exactly
        # why NETWORK_MARKERS above exists.
        if leaves and all(_is_upstream_text(str(x)) for x in leaves):
            return (
                "upstream_service_error",
                "Gemini returned a server-side error (500 internal / 503 / overloaded); the "
                "reconnect loop restores the session automatically. Not a defect in Era — but "
                "the 24h RATE is checked separately in verdict_for, so a worsening upstream "
                "is still surfaced rather than silently dismissed.",
            )

    # A gate that held is the safety layer working. The router already dismisses
    # these; sharing its list keeps one source of truth, so a type added there is
    # never counted as a live fault here.
    if event == "tool.execution.failed":
        from core.claude_error_router import BENIGN_ERROR_TYPES

        if rec.get("error_type") in BENIGN_ERROR_TYPES:
            return (
                "gate_held",
                "A confirmation gate or safety block refused the call — the system "
                "working as designed, not a fault.",
            )

    if event == "engine.network.blip":
        return (
            "network_blip",
            "Connection dropped (Wi-Fi/DNS); the reconnect loop restores the session automatically.",
        )

    if event == "engine.upstream.error":
        return (
            "upstream_service_error",
            "Gemini returned a server-side error (500 internal / 503 / overloaded); the reconnect "
            "loop restores the session automatically. The 24h rate is checked in verdict_for.",
        )

    if _is_test_origin(rec, raw):
        return (
            "test_artifact",
            "Throwaway file outside the repo (<external>/…) — a test fixture, not live data.",
        )

    if event == "memory.corruption.detected":
        if str(rec.get("status", "")).upper() == "CORRUPTED_RECOVERED":
            return (
                "memory_self_healed",
                "Real file corrupted but auto-recovered from backup — the guard did its job.",
            )
        # CORRUPTED_NO_BACKUP on a real path is genuine data loss. Never suppress.

    return None


def source_files() -> list[Path]:
    files = sorted(_REPO_ROOT.glob("*.py"))
    for d in _SOURCE_DIRS:
        root = _REPO_ROOT / d
        if root.is_dir():
            files += sorted(p for p in root.rglob("*.py") if not (_SKIP_PARTS & set(p.parts)))
    return files


def phase_syntax() -> list[str]:
    """compile() in memory: same verdict as py_compile, but writes no .pyc —
    a diagnosis must not touch the tree it is diagnosing."""
    problems = []
    for f in source_files():
        try:
            # utf-8-sig: main.py starts with a BOM, which Python accepts. Reading it
            # as plain utf-8 reported a fake "critical syntax error" (2026-09-16).
            compile(f.read_text(encoding="utf-8-sig", errors="replace"), str(f), "exec")
        except SyntaxError as e:
            problems.append(f"{f.relative_to(_REPO_ROOT).as_posix()}:{e.lineno}: {e.msg}")
        except Exception as e:
            problems.append(f"{f.relative_to(_REPO_ROOT).as_posix()}: {type(e).__name__}: {e}")
    return problems


def phase_doctor() -> tuple[str, list[str]]:
    """Run era_doctor in a subprocess. Returns (verdict, ERROR/WARN lines)."""
    doctor = _REPO_ROOT / "tools" / "era_doctor.py"
    if not doctor.exists():
        return "SKIPPED (tools/era_doctor.py not found)", []
    try:
        proc = subprocess.run(
            [sys.executable, str(doctor)],
            capture_output=True,
            text=True,
            timeout=_DOCTOR_TIMEOUT_S,
            cwd=str(_REPO_ROOT),
            encoding="utf-8",
            errors="replace",
            # Otherwise the child prints in cp1252 and every em-dash arrives as "�".
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return f"TIMED OUT after {_DOCTOR_TIMEOUT_S}s", []
    except Exception as e:
        return f"FAILED to run: {e}", []
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    findings = [ln.strip() for ln in out.splitlines() if re.match(r"\s*\[(FAIL|ERROR|WARN)\]", ln)]
    verdict = "READY" if proc.returncode == 0 else "NOT READY"
    return verdict, findings[:40]


def repo_frame(traceback_text: str) -> str:
    """Deepest traceback frame inside Era's own code, as 'path:line in func'.
    Third-party frames (sounddevice, asyncio) say where it blew up, not what
    Era should change — the deepest REPO frame is the actionable location."""
    best = ""
    root = str(_REPO_ROOT).lower()
    for path, line, func in _FRAME.findall(traceback_text or ""):
        low = path.lower()
        if low.startswith(root) and "site-packages" not in low:
            rel = Path(path).relative_to(_REPO_ROOT).as_posix()
            best = f"{rel}:{line} in {func}"
    return best


def _evidence(rec: dict) -> str:
    """The most specific error text the record holds. Innermost first."""
    tb = str(rec.get("traceback") or "")
    leaf = re.findall(r"^\s*\|?\s*([A-Za-z_][\w.]*(?:Error|Exception|Warning)[^\n]*)$", tb, re.M)
    for candidate in (
        leaf[-1] if leaf else "",
        "; ".join(rec.get("leaf_errors") or []),
        rec.get("error"),
        rec.get("message"),
    ):
        if candidate:
            return str(candidate).strip()[:300]
    return ""


_CRASH_ECHO_WINDOW_S = 2.0


def _crash_echo(
    event: str, rec: dict, ts: str, last_crash: tuple[datetime, str] | None
) -> tuple[tuple[str, str] | None, tuple[datetime, str] | None]:
    """One dropped session is logged twice: engine.crash from _receive_audio, then
    engine.reconnect_error from run_connect_loop as the SAME exception leaves the
    TaskGroup ~0.1s later. Counting both doubled every drop, so 4 upstream Gemini
    1011s read as 8 crashes and tipped the verdict to DEGRADED (2026-09-28).
    Returns (benign verdict or None, updated last-crash marker).

    Called for EVERY record, including crashes `_classify` has already explained,
    so the marker is always current — see the note at the call site."""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None, last_crash
    if event == "engine.crash":
        return None, (when, str(rec.get("error") or ""))
    if event == "engine.reconnect_error" and last_crash is not None:
        crash_at, crash_error = last_crash
        same = bool(crash_error) and crash_error in json.dumps(rec)
        if same and 0 <= (when - crash_at).total_seconds() <= _CRASH_ECHO_WINDOW_S:
            return (
                "crash_echo",
                "The reconnect loop re-logging the engine.crash just above it (same "
                "exception, same drop) — counted once, under engine.crash.",
            ), None
    return None, last_crash


def phase_logs(hours: int = _LOG_WINDOW_HOURS) -> tuple[list[dict], list[str]]:
    """Real error signatures from era.log over the last `hours`.

    Returns (real, suppressed). Each real item: {sig, count, evidence, location,
    last_seen}. `suppressed` keeps the dismissed classes WITH their reason, so the
    classifier can never quietly hide a regression.
    """
    log_file = _REPO_ROOT / "logs" / "era.log"
    if not log_file.exists():
        return [], []

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    real: Counter[str] = Counter()
    detail: dict[str, dict] = {}
    benign: Counter[str] = Counter()
    reasons: dict[str, str] = {}

    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return [], []

    last_crash: tuple[datetime, str] | None = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        rec: dict = {}
        if raw.startswith("{"):
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                rec = {}

        ts = rec.get("ts")
        if ts:
            try:
                if datetime.fromisoformat(str(ts).replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                pass

        level = str(rec.get("level", "")).upper()
        event = str(rec.get("event", ""))
        is_error = level in ("ERROR", "CRITICAL") or (not rec and _ERROR_LINE.search(raw))
        verdict = _classify(event, level, rec, raw)
        if rec and ts:
            # Run UNCONDITIONALLY, and let the echo verdict win. It used to run
            # only for crashes _classify had not already explained, which was
            # fine while every suppressed class was rare — but once an
            # engine.crash could be classified (upstream_service_error, 2026-09-30)
            # the crash set no `last_crash` marker, so its echo 0.1s later was
            # counted a SECOND time under that same class. That is the very
            # double-count this function exists to stop, reappearing one level up:
            # 4 drops would read as 8, and the upstream rate note would fire at
            # half its intended threshold.
            echo, last_crash = _crash_echo(event, rec, str(ts), last_crash)
            verdict = echo or verdict
        if verdict is not None:
            benign[verdict[0]] += 1
            reasons[verdict[0]] = verdict[1]
            continue
        if not is_error:
            continue

        if rec:
            sig = f"{event or 'unknown'} [{rec.get('error_type') or level}]"
            if rec.get("tool"):
                sig += f" tool={rec['tool']}"
        else:
            sig = re.sub(r"\d[\d:\-.TZ ]{4,}", "<t>", raw)[:160]
        real[sig] += 1
        d = detail.setdefault(sig, {"evidence": "", "location": "", "last_seen": ""})
        # Newest wins, but never overwrite real evidence with an empty record.
        ev = _evidence(rec) if rec else raw[:300]
        if ev:
            d["evidence"] = ev
        loc = repo_frame(str(rec.get("traceback") or ""))
        if loc:
            d["location"] = loc
        d["last_seen"] = str(ts or d["last_seen"])

    real_out = [{"sig": sig, "count": n, **detail[sig]} for sig, n in real.most_common(15)]
    benign_out = [f"{n}x  {cls} — {reasons[cls]}" for cls, n in benign.most_common()]
    return real_out, benign_out


def verdict_for(
    syntax: list[str],
    doctor_verdict: str,
    logs: list[dict],
    suppressed: list[str] | None = None,
) -> str:
    """Computed, never model-written. A handful of tool failures is not 'critical'.

    `suppressed` is optional so existing callers keep working, but passing it is
    what stops a dismissal from becoming a blind spot: upstream 5xx no longer
    count as crashes (they are not Era failing), and without the rate check below
    a provider getting steadily worse would read as a clean bill of health.
    """
    if syntax or doctor_verdict.startswith("NOT READY"):
        return "BROKEN — Era may not start or a core check fails"
    crashes = sum(
        i["count"] for i in logs if i["sig"].startswith(("engine.crash", "engine.reconnect_error"))
    )
    total = sum(i["count"] for i in logs)
    if crashes >= 5 or total >= 50:
        return "DEGRADED — recurring engine or tool faults"

    notes = []
    upstream = suppressed_count(suppressed, _UPSTREAM_BENIGN_CLASS)
    if upstream >= _UPSTREAM_RATE_NOTE:
        # Recovered, so not DEGRADED — Era did its job every time. But 18 of these
        # is 18 interrupted conversations, and saying nothing would trade one false
        # alarm for one blind spot.
        notes.append(
            f"{upstream} upstream provider errors in {_LOG_WINDOW_HOURS}h, every one "
            "recovered by the reconnect loop — not an Era fault, but that rate is "
            "worth a look (network, region, or the Live model)"
        )
    if total:
        notes.append("isolated failures worth a look")
    if notes:
        return "HEALTHY WITH ISSUES — " + "; ".join(notes)
    return "HEALTHY — no unexplained errors"


_ALREADY_IMPLEMENTED = """\
- main.py / run_connect_loop: reconnect with exponential backoff + jitter, a
  consecutive-failure cap that falls back to standby, GoAway handling, session
  resumption. The ~2.5min 1008 rotation is expected and NOT a crash.
- memory/safe_store.py: writes are ATOMIC (mkstemp + os.replace) with rotating
  backups and auto-recovery.
- core/pending_confirmation.py: single process-wide store via get_shared_store().
- tool.execution.failed covers ANY tool returning a failure, including a user
  cancelling a confirmation or a file that does not exist — not only bugs.
- The reconnect loop already guards its own teardown: "cannot schedule new
  futures after interpreter shutdown" is Python finalizing, not a missing lock,
  a race, or a memory leak. Never propose adding locks, threading changes or
  memory management for it — a lock is a standard defensive pattern, not the fix.
"""


def _repo_paths_in(text: str) -> list[str]:
    return re.findall(r"[\w./\\-]+\.py", text or "")


def phase_analyze(logs: list[dict], syntax: list[str], doctor: list[str]) -> list[dict]:
    """Model explains each REAL signature. Output is validated, never trusted."""
    if not (logs or syntax or doctor):
        return []
    from core.ai_provider import DEFAULT_SMART_MODEL, generate_json

    given = {i["sig"] for i in logs}
    payload = {
        "syntax_errors": syntax,
        "doctor_findings": doctor,
        "log_signatures": [
            {k: i[k] for k in ("sig", "count", "evidence", "location")} for i in logs
        ],
    }
    result = generate_json(
        "Era One is a Python voice assistant (asyncio + Gemini Live + PyQt6 UI).\n"
        f"ALREADY IMPLEMENTED — never propose building these:\n{_ALREADY_IMPLEMENTED}\n"
        "Explain each log signature below using ONLY its evidence and location. "
        'Return JSON: {"items": [{"sig": <exactly one sig from the input>, '
        '"meaning": str, "root_cause": str, "fix": str, "file": <repo path or "">, '
        '"confidence": "high"|"medium"|"low"}]}. If the evidence does not show a '
        "cause, say so and set confidence low. Never name a file that is not in the "
        f"location field.\n\nEVIDENCE:\n{json.dumps(payload)[:8000]}",
        model=DEFAULT_SMART_MODEL,
        system_instruction=(
            "You are a senior Python reliability engineer. You are rigorous about "
            "evidence and say 'unknown' rather than guess."
        ),
        fallback={"items": []},
    )
    locations = {i["sig"]: i.get("location", "") for i in logs}
    out = []
    for item in (result or {}).get("items") or []:
        if not isinstance(item, dict) or item.get("sig") not in given:
            continue  # the model invented a signature — drop it
        note = ""
        cited = str(item.get("file") or "")
        for p in _repo_paths_in(cited + " " + str(item.get("fix", ""))):
            if not (_REPO_ROOT / p.replace("\\", "/")).exists():
                note = f"(model cited non-existent `{p}`; corrected to the logged location)"
                item["file"] = locations.get(item["sig"], "")
                break
        item["validation"] = note
        out.append(item)
    return out


def render_analysis(items: list[dict]) -> list[str]:
    lines = []
    for i in items:
        lines += [
            f"- **{i['sig']}** — confidence {i.get('confidence', '?')}",
            f"  - Meaning: {i.get('meaning', '')}",
            f"  - Root cause: {i.get('root_cause', '')}",
            f"  - Fix: {i.get('fix', '')}" + (f" (`{i['file']}`)" if i.get("file") else ""),
        ]
        if i.get("validation"):
            lines.append(f"  - ⚠ {i['validation']}")
    return lines or ["(no model analysis — evidence above stands on its own)"]


def run_self_diagnosis(parameters: dict | None = None, player=None, speak=None) -> str:
    """Entry point (sync; main.py runs it in a background thread)."""
    params = parameters or {}
    deep = True if params.get("deep") is None else bool(params.get("deep"))

    log("DIAG", "self-diagnosis started (syntax sweep)...", player)
    syntax = phase_syntax()
    doctor_verdict, doctor = phase_doctor()
    logs, suppressed = phase_logs()
    verdict = verdict_for(syntax, doctor_verdict, logs, suppressed)
    analysis: list[dict] = []
    if deep:
        try:
            analysis = phase_analyze(logs, syntax, doctor)
        except Exception as e:
            log("DIAG", f"analysis unavailable: {e}", player)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"diag_{datetime.now():%Y%m%d_%H%M%S}.md"
    log_lines = [
        f"- {i['count']}x `{i['sig']}` — {i['evidence'] or 'no error text recorded'}"
        + (f" @ `{i['location']}`" if i["location"] else "")
        for i in logs
    ]
    report = "\n".join(
        [
            f"# Era Self-Diagnosis — {datetime.now():%Y-%m-%d %H:%M}",
            "",
            f"**Verdict (computed from evidence, not model-written): {verdict}**",
            "",
            f"## Syntax sweep ({len(source_files())} files)",
            *(f"- {p}" for p in (syntax or ["No syntax errors."])),
            "",
            f"## era_doctor — {doctor_verdict}",
            *(f"- {p}" for p in (doctor or ["No FAIL/WARN findings."])),
            "",
            f"## Real log errors — last {_LOG_WINDOW_HOURS}h (deduped, true counts, evidence)",
            *(log_lines or ["- No unexplained errors in era.log."]),
            "",
            f"## Suppressed as expected — last {_LOG_WINDOW_HOURS}h",
            *(f"- {p}" for p in (suppressed or ["Nothing suppressed."])),
            "",
            "## Analysis (validated against the evidence above)",
            *(render_analysis(analysis) if deep else ["(deep analysis skipped)"]),
            "",
        ]
    )
    report_path.write_text(report, encoding="utf-8")
    log("DIAG", f"report written: {report_path}", player)

    if not (syntax or logs) and doctor_verdict == "READY":
        return (
            f"Deep diagnosis complete: {verdict}. No syntax errors, doctor is green, "
            f"no unexplained errors in the last {_LOG_WINDOW_HOURS}h."
        )
    return (
        f"Deep diagnosis complete — {verdict}. {len(syntax)} syntax problem(s), "
        f"doctor {doctor_verdict} with {len(doctor)} note(s), "
        f"{len(logs)} real error signature(s) in the last {_LOG_WINDOW_HOURS}h. "
        f"Report: {report_path}"
    )
