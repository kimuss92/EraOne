"""core/handled_exceptions.py — see exceptions Era's tools CATCH, not just the ones that escape.

Most tools end in `except Exception as e: return f"Couldn't ...: {e}"`. That
turns a code defect into a polite string: no traceback reaches era.log, so
core/self_heal — which acts only on frames Python recorded — could not see
the most common kind of failure at all.

Instead of rewriting every handler, this watches the moment a handler catches
an exception, via sys.monitoring's EXCEPTION_HANDLED event (PEP 669).

PROVENANCE (security review 2026-09-28, don't loosen): a record is only ever
attributed to the tool call it happened IN. Each call sets CURRENT_CALL (a
ContextVar) in tool_dispatch; asyncio.to_thread copies context, and
`install_context_executor` makes run_in_executor copy it too. Exceptions
caught with no call id — background threads, spies, the scheduler, polling
loops — are never recorded. Without this, text a web page put into one
tool's failure message could pick ANY exception caught anywhere in Era and
get self-heal to "fix" a working function, or roll a good fix back.

Attribution inside the call (`match`), at most one record per call:
  * the failure message ENDS with the exception text (the f"...: {e}" shape), or
  * the message contains it and the text is specific (>= _SPECIFIC_LEN chars,
    not a bare quoted key like 'data'), or
  * it is the only record caught in the tool's own module AND the message
    names its type.

Records keep (file, line, function) as plain data and a redacted, bounded
message — never the exception or frame objects. EXCEPTION_HANDLED cannot be
disabled per location (measured ~65 ns per caught exception on 3.14), so the
callback bails out on the ContextVar first, then on a filename-string cache.
"""

from __future__ import annotations

import contextvars
import itertools
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_WATCHED_DIRS = ("actions", "core", "utils", "agent")
_MAX_RECORDS = 300
_MAX_FRAMES = 40
_MIN_TAIL_LEN = 4
_SPECIFIC_LEN = 16
_MAX_CACHE = 4096
_TOOL_NAME = "era-handled-exceptions"
_QUOTED_IDENT = re.compile(r"""^['"][\w.\-]{0,40}['"]$""")

CURRENT_CALL: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "era_tool_call", default=None
)
_call_ids = itertools.count(1)

_records: deque[dict] = deque(maxlen=_MAX_RECORDS)
_lock = threading.RLock()  # re-entrant: GC inside match() can fire the callback
_install_lock = threading.Lock()
_watch_cache: dict[str, bool] = {}  # co_filename -> watched; strings, capped
_prefix_cache: tuple[str, tuple[str, ...]] = ("", ())
_tool_id: int | None = None


def new_call_id() -> int:
    return next(_call_ids)


class ContextCopyingExecutor(ThreadPoolExecutor):
    """run_in_executor(None, fn) does not copy contextvars (asyncio.to_thread
    does). Tools dispatched through it would record nothing — or, worse, the
    thread would carry nothing and the call id would be lost."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def install_context_executor(loop) -> None:
    if getattr(loop, "_era_context_executor", False):
        return
    loop.set_default_executor(ContextCopyingExecutor(thread_name_prefix="era-tool"))
    loop._era_context_executor = True


def _prefixes() -> tuple[str, ...]:
    global _prefix_cache
    root = str(REPO_ROOT)
    if _prefix_cache[0] != root:
        base = os.path.normcase(root)
        _prefix_cache = (root, tuple(os.path.join(base, d) + os.sep for d in _WATCHED_DIRS))
        _watch_cache.clear()
    return _prefix_cache[1]


def _watched(filename: str) -> bool:
    hit = _watch_cache.get(filename)
    if hit is None:
        norm = os.path.normcase(filename)
        base = os.path.basename(norm)
        hit = (
            norm.startswith(_prefixes())
            and norm != os.path.normcase(__file__)
            and not base.startswith(("test_", "conftest"))
        )
        if len(_watch_cache) >= _MAX_CACHE:
            _watch_cache.clear()
        _watch_cache[filename] = hit
    return hit


def _frames(tb) -> list[list]:
    """Innermost _MAX_FRAMES frames — the raise site is what self_heal targets."""
    out: deque[list] = deque(maxlen=_MAX_FRAMES)
    while tb is not None:
        code = tb.tb_frame.f_code
        out.append([code.co_filename, tb.tb_lineno, code.co_name])
        tb = tb.tb_next
    return list(out)


def _message(exc: BaseException) -> str:
    """Bounded and redacted BEFORE it is cut, so a key split at the cut can't
    survive half-masked. Never str() on a multi-arg exception: that reprs the
    whole tuple (a bytes payload included) before any cut."""
    from core.logger import redact_secrets

    args = getattr(exc, "args", ())
    if not args:
        raw = ""
    elif len(args) == 1 and isinstance(args[0], str) and len(args[0]) > 1000:
        raw = args[0][:1000]  # huge: never let str()/repr() walk all of it
    elif len(args) == 1 and isinstance(args[0], (str, int, float)):
        # str(), not args[0]: KeyError renders as "'key'", and that rendering
        # is what the tool's f"...: {e}" message contains.
        try:
            raw = str(exc)[:1000]
        except Exception:
            raw = type(exc).__name__
    else:
        raw = type(exc).__name__
    return redact_secrets(raw)[:300]


def _on_handled(code, _offset, exc) -> None:
    """Never raises: an error here would surface inside Era's own handlers."""
    try:
        call = CURRENT_CALL.get()
        if call is None or not _watched(code.co_filename):
            return
        frames = _frames(exc.__traceback__)
        if not frames:
            return
        msg = _message(exc)
        etype = type(exc).__name__
        now = time.perf_counter()
        with _lock:
            for prev in reversed(_records):
                if prev["call"] != call:
                    continue
                # The same exception caught again further out: same call, type,
                # message and raise site. id(exc) alone was reused across objects.
                if (
                    prev["type"] == etype
                    and prev["msg"] == msg
                    and prev["frames"][-1] == frames[-1]
                ):
                    if len(frames) >= len(prev["frames"]):
                        prev["frames"] = frames
                    prev["handler"], prev["ts"] = code.co_filename, now
                    return
                break
            _records.append(
                {
                    "ts": now,
                    "call": call,
                    "type": etype,
                    "msg": msg,
                    "frames": frames,
                    "handler": code.co_filename,
                }
            )
    except Exception:
        pass


def install() -> bool:
    """Start watching. Idempotent and never raises; False when no monitoring
    slot is free (a debugger/profiler/coverage holds them) or no PEP 669."""
    global _tool_id
    with _install_lock:
        if _tool_id is not None:
            return True
        mon = getattr(sys, "monitoring", None)
        if mon is None:
            return False
        for tid in (3, 4):  # 0/1/2/5: debugger/coverage/profiler/optimizer
            try:
                if mon.get_tool(tid) is not None:
                    continue
                mon.use_tool_id(tid, _TOOL_NAME)
            except ValueError:
                continue  # lost a race for the slot — try the next one
            try:
                mon.register_callback(tid, mon.events.EXCEPTION_HANDLED, _on_handled)
                mon.set_events(tid, mon.get_events(tid) | mon.events.EXCEPTION_HANDLED)
            except Exception:
                mon.free_tool_id(tid)
                return False
            _tool_id = tid
            return True
        return False


def uninstall() -> None:
    global _tool_id
    with _install_lock:
        mon = getattr(sys, "monitoring", None)
        if mon is None or _tool_id is None:
            return
        mon.set_events(_tool_id, 0)
        mon.register_callback(_tool_id, mon.events.EXCEPTION_HANDLED, None)
        mon.free_tool_id(_tool_id)
        _tool_id = None


def installed() -> bool:
    return _tool_id is not None


def _specific(msg: str) -> bool:
    return len(msg) >= _SPECIFIC_LEN and not _QUOTED_IDENT.match(msg)


def match(call: int | None, message: str, module_hint: str = "") -> dict | None:
    """The caught exception behind THIS failed tool call, or None."""
    if call is None:
        return None
    text = (message or "").rstrip()
    mine = [dict(r) for r in list(_records) if r["call"] == call]  # snapshot, no lock
    tail = [r for r in mine if len(r["msg"]) >= _MIN_TAIL_LEN and text.endswith(r["msg"])]
    inside = [r for r in mine if _specific(r["msg"]) and r["msg"] in text]
    for found, how in ((tail, "message_tail"), (inside, "message")):
        if found:
            best = max(found, key=lambda r: (len(r["msg"]), r["ts"]))
            best["correlation"] = how
            return best
    if module_hint:
        hint = module_hint.replace("\\", "/").lower()
        own = [r for r in mine if hint in r["handler"].replace("\\", "/").lower()]
        if len(own) == 1 and re.search(rf"\b{re.escape(own[0]['type'])}\b", text):
            own[0]["correlation"] = "module"
            return own[0]
    return None


def _reset_for_tests() -> None:
    global _prefix_cache
    with _lock:
        _records.clear()
    _watch_cache.clear()
    _prefix_cache = ("", ())
