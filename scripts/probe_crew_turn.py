"""Diagnostic: run ONE crew turn through the configured transport and print every
capability the bot calls (name, args, size of what it got back) and its final answer.

Same code path Era uses for a crew request (Hermes first, PF-046 fallback). The bot's
own capability gates still apply: staging needs approval and nothing is applied.

Usage (Windows Python, from the repo root):
    python scripts/probe_crew_turn.py <bot> "task text"
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crew import fallback  # noqa: E402
from crew import runtime as rt  # noqa: E402
from crew.transport import Turn  # noqa: E402


def main() -> int:
    bot, task = sys.argv[1], sys.argv[2]
    transport = rt.get_transport()
    real_execute = rt.InProcessTransport._execute

    def spy(self, spec, turn, name, args):
        result = real_execute(self, spec, turn, name, args)
        content = (result.data or {}).get("content") if isinstance(result.data, dict) else None
        print(
            f"  CALL {spec.id} -> {name} {json.dumps(args)[:300]} "
            f"ok={result.success} returned_chars={len(content or result.message or '')}"
        )
        return result

    rt.InProcessTransport._execute = spy
    try:
        from crew.hermes_transport import HermesAgentTransport

        real_ask = HermesAgentTransport._ask

        def ask_spy(self, spec, turn, system, user):
            reply = real_ask(self, spec, turn, system, user)
            kind = next(iter(reply), "?") if isinstance(reply, dict) else "?"
            detail = str(reply.get("__bad_output__", ""))[:160] if kind == "__bad_output__" else ""
            print(f"  HERMES {spec.id} reply={kind} prompt_chars={len(user)} {detail}")
            return reply

        HermesAgentTransport._ask = ask_spy
        from crew import hermes_transport as ht

        real_parse = ht.parse_crew_reply

        def parse_spy(text):
            obj = real_parse(text)
            if obj is None and isinstance(text, str) and "{" in text:
                try:
                    start, end = text.find("{"), text.rfind("}")
                    json.loads(text[start : end + 1])
                except ValueError as exc:
                    where = getattr(exc, "pos", 0) or 0
                    print(
                        f"  UNPARSEABLE ({exc}); around: {text[max(0, where - 120) : where + 80]!r}"
                    )
            return obj

        ht.parse_crew_reply = parse_spy
    except ImportError:
        pass
    if os.environ.get("PROBE_DRY_STAGE") == "1":
        from core.tool_result import ToolResult
        from crew import capabilities as caps

        def dry_stage(_ctx, args):
            """Run the real candidate checks, queue nothing; a valid one ends the turn."""
            import re as _re

            from core import development as dev
            from crew import source

            print(f"  DRY source.stage args={json.dumps(args)[:1500]}")
            try:
                files = source._expand_edits(args.get("files"))
                if not isinstance(files, list) or not 1 <= len(files) <= 6:
                    raise ValueError("A candidate requires 1-6 files")
                staged = set()
                for item in files:
                    if not isinstance(item, dict) or "base_sha256" not in item:
                        raise ValueError("Each file needs its observed base_sha256, or null")
                    if dev._hash(dev._path(item.get("path"))) != item["base_sha256"]:
                        raise ValueError(f"Source changed; read {item.get('path')} again")
                    if not isinstance(item.get("content"), str):
                        raise ValueError("Candidate paths must contain complete source")
                    if item["path"].endswith(".py"):
                        compile(item["content"], item["path"], "exec")
                    staged.add(item["path"])
                tests = args.get("tests")
                if not isinstance(tests, list) or not 1 <= len(tests) <= 6:
                    raise ValueError("Give 1-6 focused tests")
                if not all(_re.fullmatch(r"tools/test_[a-zA-Z0-9_]+\.py", t) for t in tests):
                    raise ValueError("Tests must be tools/test_*.py files")
                if not any(t in staged for t in tests):
                    raise ValueError("The candidate must include a new or updated test")
            except (ValueError, OSError, SyntaxError) as exc:
                print(f"  DRY REFUSED: {exc}")
                return ToolResult.fail(message=str(exc), error_type="SourceConflict")
            print("  DRY VALID candidate:")
            for item in files:
                print(f"    --- {item['path']} ---\n{item['content'][-1600:]}")
            return ToolResult.fail(
                "DRY RUN: a valid candidate; nothing was queued.", error_type="ConfirmationRequired"
            )

        caps.CAPABILITIES["source.stage"] = caps.CAPABILITIES["source.stage"].__class__(
            "source.stage",
            caps.CAPABILITIES["source.stage"].description,
            caps.CAPABILITIES["source.stage"].args,
            dry_stage,
        )
    started = time.monotonic()
    result, used = fallback.dispatch(transport, bot, Turn(task=task))
    print(
        f"transport={getattr(used, 'name', '?')} ok={result.success} "
        f"error={result.error_type} seconds={time.monotonic() - started:.1f}"
    )
    print("ANSWER >>>")
    print((result.message or "")[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
