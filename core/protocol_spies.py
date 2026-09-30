"""core/protocol_spies.py — the low-impact intelligence network behind Protocol One.

Four passive watchers run on ONE daemon thread inside Era and post what they see
to the hive (core/hive.py). Protocol One reads that map the moment it launches,
so it starts from what has already been noticed instead of from zero.

  LogSpy        new era.log lines only (byte offset) -> real error signatures
  SourceSpy     .py files whose mtime changed -> compile + secret patterns
  StateSpy      memory/*.json whose mtime changed -> still valid JSON?
  ToolchainSpy  hourly: can the interpreter running Era actually run ruff?
  DriftSpy      prompt.txt / tool_declarations.py moved -> Protocol Zero drift check
  VSCodeSpy     every 20 s: did VS Code just open on this project? -> run Protocol Zero

WHY THESE ARE NOT LLM AGENTS
  "Continuous background agents" built from model calls would burn quota around
  the clock (see CLAUDE.md on the ruflo daemon) and compete with Era's own voice
  session. Every spy here is plain Python doing stat() calls and reading deltas:
  a quiet tick costs a few milliseconds. The model is only used by Protocol One,
  and only when you launch it.

EFFICIENCY RULES
  * Nothing is ever re-read unless its size/mtime moved.
  * First tick waits 2 minutes so it never competes with Era's startup.
  * Each tick records its own duration in the hive status — the cost is measured,
    not asserted.
  * A spy that raises is recorded and skipped; it never kills the thread.

A spy RESOLVES what it raised once it re-checks and the condition is gone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from core import hive

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_INTERVAL_S = 300
_STARTUP_DELAY_S = 120
_MAX_LOG_BYTES_PER_TICK = 2_000_000
_TOOLCHAIN_EVERY_S = 3600
_FAST_INTERVAL_S = 20  # the VS Code launch check; everything else runs every interval_s

# Credentials that must never appear in source. config/api_keys.json is the
# sanctioned local store and is not scanned; everything under _SOURCE_DIRS is.
SECRET_PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Anthropic API key": re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}"),
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{33}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
}
_SOURCE_DIRS = ("actions", "agent", "app", "core", "utils", "windows")


def _rel(p: Path) -> str:
    try:
        return p.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return str(p)


def scan_secrets(text: str) -> list[str]:
    """Names of credential types found. Never returns the secret itself."""
    return [name for name, rx in SECRET_PATTERNS.items() if rx.search(text)]


class LogSpy:
    name = "log"

    def __init__(self, log_file: Path | None = None) -> None:
        self.log_file = log_file or _REPO_ROOT / "logs" / "era.log"
        self.offset: int | None = None

    def tick(self) -> int:
        from actions.self_diagnosis import _classify, _evidence, repo_frame

        try:
            size = self.log_file.stat().st_size
        except OSError:
            return 0
        if self.offset is None or size < self.offset:
            # First sight (history belongs to self_diagnosis) or the log rotated.
            self.offset = size
            return 0
        if size == self.offset:
            return 0
        start = max(self.offset, size - _MAX_LOG_BYTES_PER_TICK)
        with open(self.log_file, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start).decode("utf-8", errors="replace")
        self.offset = size

        posted = 0
        for raw in chunk.splitlines():
            if not raw.startswith("{"):
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            level = str(rec.get("level", "")).upper()
            if level not in ("ERROR", "CRITICAL"):
                continue
            event = str(rec.get("event", ""))
            if _classify(event, level, rec, raw) is not None:
                continue
            sig = f"log:{event} [{rec.get('error_type') or level}]"
            if rec.get("tool"):
                sig += f" tool={rec['tool']}"
            sev = "high" if event.startswith(("engine.crash", "memory.corruption")) else "medium"
            hive.post_insight(
                "spies",
                sig,
                severity=sev,
                category="runtime_error",
                title=sig[4:],
                evidence=_evidence(rec),
                location=repo_frame(str(rec.get("traceback") or "")),
            )
            posted += 1
        return posted


class SourceSpy:
    name = "source"

    def __init__(self, dirs: tuple[str, ...] = _SOURCE_DIRS, root: Path = _REPO_ROOT) -> None:
        self.root = root
        self.dirs = dirs
        self.seen: dict[str, float] = {}

    def _files(self):
        yield from self.root.glob("*.py")
        for d in self.dirs:
            base = self.root / d
            if base.is_dir():
                for p in base.rglob("*.py"):
                    if "__pycache__" not in p.parts:
                        yield p

    def tick(self) -> int:
        changed = 0
        for p in self._files():
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            key = str(p)
            if self.seen.get(key) == mtime:
                continue
            self.seen[key] = mtime
            changed += 1
            self.check(p)
        return changed

    def check(self, p: Path) -> None:
        rel = _rel(p)
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")  # BOM-safe
        except OSError:
            return
        sig = f"syntax:{rel}"
        try:
            compile(text, str(p), "exec")
            hive.resolve_insight(sig, by="spies", note="compiles again")
        except SyntaxError as e:
            hive.post_insight(
                "spies",
                sig,
                severity="critical",
                category="fatal",
                title=f"Syntax error: {e.msg}",
                evidence=(e.text or "").strip()[:200],
                location=f"{rel}:{e.lineno}",
            )
        found = scan_secrets(text)
        sig = f"secret:{rel}"
        if found:
            hive.post_insight(
                "spies",
                sig,
                severity="critical",
                category="security",
                title=f"Credential in source: {', '.join(found)}",
                evidence="Value withheld. Move it to config/api_keys.json and rotate it.",
                location=rel,
            )
        else:
            hive.resolve_insight(sig, by="spies", note="no credential pattern any more")


class StateSpy:
    name = "state"

    def __init__(self, memory_dir: Path | None = None) -> None:
        self.memory_dir = memory_dir or _REPO_ROOT / "memory"
        self.seen: dict[str, float] = {}

    def tick(self) -> int:
        changed = 0
        for p in self.memory_dir.glob("*.json"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if self.seen.get(str(p)) == mtime:
                continue
            self.seen[str(p)] = mtime
            changed += 1
            sig = f"state:{p.name}"
            try:
                json.loads(p.read_text(encoding="utf-8"))
                hive.resolve_insight(sig, by="spies", note="valid JSON again")
            except (OSError, ValueError) as e:
                # The file may be mid-write; only a second failing look counts.
                time.sleep(0.5)
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    hive.post_insight(
                        "spies",
                        sig,
                        severity="high",
                        category="data",
                        title=f"Memory state file is not valid JSON: {p.name}",
                        evidence=str(e)[:200],
                        location=_rel(p),
                    )
        return changed


class ToolchainSpy:
    """ruff missing from the interpreter running Era is silent: self_improve's
    lint gate reports UNAVAILABLE and passes, the format hook no-ops. Found live
    2026-09-16 — C:\\Python314 had no ruff while another install did."""

    name = "toolchain"

    def __init__(self) -> None:
        self.last = 0.0

    def tick(self) -> int:
        if time.time() - self.last < _TOOLCHAIN_EVERY_S:
            return 0
        self.last = time.time()
        sig = "toolchain:ruff"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "ruff", "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            hive.resolve_insight(sig, by="spies", note="ruff runs under Era's interpreter")
        else:
            hive.post_insight(
                "spies",
                sig,
                severity="high",
                category="toolchain",
                title="ruff is not installed for the interpreter running Era",
                evidence=f"{sys.executable} -m ruff failed; lint gates silently pass",
                location=sys.executable,
            )
        return 1


class DriftSpy:
    """Protocol Zero's continuous audit. Re-checks only when prompt.txt or the
    declarations move, and only READS: it never writes the prompt."""

    name = "prompt_drift"

    def __init__(self, paths: tuple[Path, ...] | None = None, check=None) -> None:
        self.paths = paths or (
            _REPO_ROOT / "core" / "prompt.txt",
            _REPO_ROOT / "app" / "tool_declarations.py",
        )
        self.check = check
        self.seen: tuple | None = None

    def tick(self) -> int:
        try:
            stamp = tuple(p.stat().st_mtime for p in self.paths)
        except OSError:
            return 0
        if stamp == self.seen:
            return 0
        self.seen = stamp
        if self.check is None:
            from actions.protocol_zero import check_drift

            self.check = check_drift
        self.check()
        return 1


def visible_window_titles() -> list[str]:
    try:
        import win32gui
    except ImportError:
        return []
    titles: list[str] = []

    def _collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            text = win32gui.GetWindowText(hwnd)
            if text:
                titles.append(text)

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception:
        return []
    return titles


class VSCodeSpy:
    """Protocol Zero trigger (b): VS Code opening ON THIS PROJECT.

    VS Code's default window title is "<file> - <folder> - Visual Studio Code",
    so the folder name in its title is the project marker. Fires on closed -> open
    only (a window already open at Era startup is not a launch), with a 30-minute
    cooldown. One EnumWindows pass costs about a millisecond."""

    name = "vscode"
    fast = True

    def __init__(
        self,
        on_open: Callable[[], None] | None = None,
        titles_fn: Callable[[], list[str]] | None = None,
        project: str = _REPO_ROOT.name,
        cooldown_s: float = 1800,
    ) -> None:
        self.on_open = on_open
        self.titles_fn = titles_fn or visible_window_titles
        self.rx = re.compile(rf"(?<![\w-]){re.escape(project)}(?![\w-]).*Visual Studio Code")
        self.cooldown_s = cooldown_s
        self.last_fired: float | None = None
        self.was_open = self._open()

    def _open(self) -> bool:
        return any(self.rx.search(t) for t in self.titles_fn())

    def tick(self) -> int:
        now_open = self._open()
        launched = now_open and not self.was_open
        self.was_open = now_open
        if not (launched and self.on_open):
            return 0
        if self.last_fired is not None and time.monotonic() - self.last_fired < self.cooldown_s:
            return 0
        self.last_fired = time.monotonic()
        self.on_open()
        return 1


class SpyNetwork:
    def __init__(
        self,
        spies: list | None = None,
        interval_s: float = _DEFAULT_INTERVAL_S,
        on_critical: Callable[[dict], None] | None = None,
        on_zero_report: Callable[[str], None] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.on_critical = on_critical
        self.on_zero_report = on_zero_report
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._announced: set[str] = set()
        self.spies = (
            spies
            if spies is not None
            else [
                LogSpy(),
                SourceSpy(),
                StateSpy(),
                ToolchainSpy(),
                DriftSpy(),
                VSCodeSpy(on_open=self.launch_protocol_zero),
            ]
        )

    def launch_protocol_zero(self) -> None:
        """Run Zero off the spy thread, so a sync never delays the next check."""

        def _run() -> None:
            from actions.protocol_zero import execute_protocol_zero

            summary = execute_protocol_zero(trigger="vscode")
            if self.on_zero_report:
                try:
                    self.on_zero_report(summary)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name="ProtocolZero").start()

    def _run_spies(self, fast: bool) -> dict:
        report: dict = {}
        for spy in self.spies:
            if bool(getattr(spy, "fast", False)) != fast:
                continue
            try:
                report[spy.name] = spy.tick()
            except Exception as e:
                report[spy.name] = f"error: {type(e).__name__}: {e}"[:200]
        return report

    def fast_tick(self) -> dict:
        return self._run_spies(fast=True)

    def tick(self) -> dict:
        started = time.perf_counter()
        report = self._run_spies(fast=False)
        ms = int((time.perf_counter() - started) * 1000)
        crit = hive.open_insights(min_severity="critical")
        hive.set_status(
            "spies",
            "watching",
            f"last tick {ms} ms; {len(hive.open_insights(min_severity='medium'))} open issue(s)",
            tick_ms=ms,
            tick=report,
        )
        if self.on_critical:
            for item in crit:
                if item["signature"] not in self._announced:
                    self._announced.add(item["signature"])
                    try:
                        self.on_critical(item)
                    except Exception:
                        pass
        return report

    def _run(self) -> None:
        if self._stop.wait(_STARTUP_DELAY_S):
            return
        last_slow = float("-inf")
        while not self._stop.is_set():
            self.fast_tick()
            if time.monotonic() - last_slow >= self.interval_s:
                last_slow = time.monotonic()
                self.tick()
            self._stop.wait(_FAST_INTERVAL_S)

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ProtocolSpies")
        self._thread.start()
        hive.set_status("spies", "starting", f"first tick in {_STARTUP_DELAY_S}s")
        return True

    def stop(self) -> None:
        self._stop.set()
        hive.set_status("spies", "stopped")


_network: SpyNetwork | None = None


def start_network(
    on_critical: Callable[[dict], None] | None = None,
    on_zero_report: Callable[[str], None] | None = None,
) -> SpyNetwork | None:
    """Start the shared network once. Never raises. ERA_SPIES=0 turns it off."""
    global _network
    if os.environ.get("ERA_SPIES", "1").strip() == "0":
        return None
    try:
        if _network is None:
            _network = SpyNetwork(on_critical=on_critical, on_zero_report=on_zero_report)
        _network.start()
        return _network
    except Exception as e:
        print(f"[SPIES] could not start: {e}")
        return None


def network() -> SpyNetwork | None:
    return _network
