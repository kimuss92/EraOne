"""Tool-execution half of the HUD bus: real tool calls in, activity rows out.

Mixed into HudBus (app/hud/events.py); relies on its lock, ring buffers and
``_emit``/``_poke``. Every public method swallows its own errors.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.hud import tool_adapter
from app.hud.bus_const import MAX_TOOLS


class ToolEventsMixin:
    @staticmethod
    def _public_run(run: dict) -> dict:
        return {k: v for k, v in run.items() if not k.startswith("_")}

    def tool_started(
        self,
        call_id: str | None,
        tool: str,
        args: Any = None,
        *,
        background: bool = False,
        source: str = "model",
    ) -> str:
        cid = str(call_id or "") or f"t{next(self._seq)}"
        try:
            with self._lock:
                if cid in self._tools and self._tools[cid]["status"] == "running":
                    cid = f"{cid}-{next(self._seq)}"
                d = tool_adapter.describe(tool, args)
                run = {
                    "id": cid,
                    "tool": tool,
                    "label": d["label"],
                    "category": d["category"],
                    "activity": d["activity"],
                    "args": tool_adapter.safe_args(args),
                    "status": "running",
                    "startedAt": time.time(),
                    "duration": None,
                    "summary": "",
                    "background": bool(background),
                    "source": source,
                    "_mono": time.monotonic(),
                }
                self._tools[cid] = run
                while len(self._tools) > MAX_TOOLS:
                    self._tools.popitem(last=False)
                self._emit(
                    "tool.started",
                    toolName=tool,
                    toolCallId=cid,
                    label=d["label"],
                    message=d["activity"],
                    metadata=self._public_run(run),
                )
                self._poke()
        except Exception:
            pass
        return cid

    def tool_cancelled(self, call_id: str, reason: str = "") -> None:
        """Close a running row as cancelled NOW, without waiting for a result.

        The owner clicked the X (or said "cancel that") and must see the row go
        terminal immediately -- not in 45 seconds when the uninterruptible
        worker thread finally returns. `cancelled` is a status the frontend
        already renders (ui_src/src/components/ActivityPanel.tsx) and
        `tool.cancelled` an event transport.ts already handles, so nothing
        downstream needs to change.

        This terminal state IS the soft-cancel discard discriminator -- the
        same job SpeechOut.interrupt's generation counter does for in-flight
        TTS, expressed as state instead of a counter. See the guard at the top
        of tool_finished.
        """
        try:
            with self._lock:
                run = self._tools.get(call_id)
                if run is None or run["status"] != "running":
                    return  # nothing to cancel, or already settled
                dur = (time.monotonic() - run["_mono"]) * 1000
                run.update(
                    status="cancelled",
                    duration=int(dur),
                    summary=reason or "Cancelled.",
                    finishedAt=time.time(),
                )
                self._emit(
                    "tool.cancelled",
                    toolName=run["tool"],
                    toolCallId=call_id,
                    label=run["label"],
                    message=run["summary"],
                    duration=int(dur),
                    metadata=self._public_run(run),
                )
                # A cancel can land while a confirmation is on screen -- the
                # cancelled call may be the very one that armed it.
                if self._approval is not None:
                    self._sync_approval()
                self._poke()
        except Exception:
            pass

    def tool_finished(
        self,
        call_id: str,
        tool: str,
        args: Any,
        result: Any,
        *,
        routed_to: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        try:
            with self._lock:
                run = self._tools.get(call_id)
                if run is None:
                    call_id = self.tool_started(call_id, tool, args)
                    run = self._tools.get(call_id)
                    if run is None:
                        return
                # A late result for a row the owner already abandoned. The
                # worker thread could not be interrupted so it ran to
                # completion anyway, but `cancelled` is terminal and this
                # result is unwanted. Race-free: both paths hold self._lock.
                if run["status"] == "cancelled":
                    return
                now_ts = time.time()
                info = tool_adapter.summarize(tool, args, result, call_id=call_id, ts=now_ts)
                dur = (
                    duration_ms if duration_ms is not None else getattr(result, "duration_ms", None)
                )
                if dur is None:
                    dur = (time.monotonic() - run["_mono"]) * 1000
                run.update(
                    status=info["status"],
                    duration=int(dur),
                    summary=info["summary"],
                    finishedAt=now_ts,
                )
                if routed_to and routed_to != tool:
                    run["routedTo"] = routed_to
                self._tool_latency[tool] = int(dur)
                status = info["status"]
                etype = {
                    "success": "tool.completed",
                    "awaiting": "tool.progress",
                    "cancelled": "tool.cancelled",
                }.get(status, "tool.failed")
                self._emit(
                    etype,
                    toolName=tool,
                    toolCallId=call_id,
                    label=run["label"],
                    message=info["summary"],
                    duration=int(dur),
                    metadata=self._public_run(run),
                )
                for c in info["cards"]:
                    self._cards.append(c)
                    self._emit("context.added", metadata={"card": c})
                for s in info["sources"]:
                    self._add_source(dict(s, origin=f"tool:{tool}", toolCallId=call_id))
                if info["memory"]:
                    self._note_memory(info["memory"], now_ts)
                if status == "failed":
                    self._failures.append(
                        {"ts": now_ts, "where": f"tool:{tool}", "message": info["summary"]}
                    )
                    self._set_transient("error", f"{run['label']} failed")
                # A new confirmation, or any tool finishing while one is shown
                # (confirm / cancel / an unrelated call that replaced it).
                if status == "awaiting" or self._approval is not None:
                    self._sync_approval()
                self._poke()
        except Exception:
            pass

    def _note_memory(self, entries: list[dict], ts: float) -> None:
        n = len(entries)
        self._memory.update(
            retrieved_total=self._memory["retrieved_total"] + n,
            last_count=n,
            last_at=ts,
            entries=[{"category": e["category"], "key": e["key"]} for e in entries],
        )
        self._emit("memory.retrieved", message=f"{n} memories", metadata={"count": n})

    def _add_source(self, src: dict) -> None:
        src.setdefault("id", uuid.uuid4().hex[:10])
        src.setdefault("timestamp", time.time())
        self._sources.append(src)
        self._emit("source.added", metadata={"source": src})

    def grounding(self, metadata: Any) -> None:
        """Web citations from Gemini's own grounding_metadata."""
        try:
            with self._lock:
                known = {s.get("url") for s in self._sources if s.get("url")}
                for src in tool_adapter.grounding_sources(metadata):
                    if src.get("url") in known:
                        continue
                    self._add_source(dict(src, origin="model grounding"))
        except Exception:
            pass

    def active_tools(self) -> list[dict]:
        with self._lock:
            return [self._public_run(r) for r in self._tools.values() if r["status"] == "running"]
