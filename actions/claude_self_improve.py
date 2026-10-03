"""Claude-Code-as-generator for self_improve.

self_improve._apply accepts a swappable `generate=` callable —
`generate(target, original, instruction) -> str`. Protocol One already uses it
(via _deep_generate in actions/protocol_one.py), but that path is buried inside
a ~12-minute maintenance cycle. This file exposes the same seam on demand, with
the tool-less Claude Code CLI as the generator.

The generator is deliberately tool-free (`--allowedTools ""` in
core/llm/adapters/claude_cli.py._build_command) so Claude cannot issue tool_use
blocks — every return is raw text that self_improve verifies through its
backup/verify/rollback pipeline. The existing safety layer (protected globs,
SAFETY_CRITICAL, size limit, compile + ruff + pytest, auto-rollback) is
unchanged; only the text-generation origin is swapped.

Nothing here writes a repo file. self_improve._apply owns backup, verification
and rollback — this module only produces candidate text.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.utils import log

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strip_fences(code: str) -> str:
    """Models wrap code in markdown fences no matter how firmly you ask."""
    code = (code or "").strip()
    for fence in ("```python", "```"):
        if code.startswith(fence):
            code = code[len(fence) :]
    if code.endswith("```"):
        code = code[:-3]
    return code.strip() + "\n"


def _generate_via_claude_cli(target: Path, original: str, instruction: str, *, allow_fallback: bool = True) -> str:
    """Generate updated code using the tool-less Claude Code CLI.

    Mirrors _build_command in core/llm/adapters/claude_cli.py:
        claude -p --output-format json --allowedTools ""
    so Claude can emit no tool_use blocks — we only ever get back raw text,
    which self_improve then verifies. If the CLI is unavailable or the daily
    budget is spent, this falls back to the Gemini route that
    self_improve._generate_updated_code already uses.
    """
    from core.llm.adapters.claude_cli import ClaudeCliProvider
    from core.llm.spend import from_registry
    from core.llm.types import LlmMessage, LlmRequest, PromptBlock

    rel = target.relative_to(REPO_ROOT).as_posix()
    ledger = from_registry()

    try:
        ok, why = ledger.can_afford(0.75)  # same estimate protocol_one uses
        if not ok:
            log("ClaudeSelfImprove", f"Claude CLI skipped: {why}")
            if not allow_fallback:
                raise RuntimeError("Claude coding budget is unavailable")
            return _fallback(target, original, instruction, rel)

        provider = ClaudeCliProvider()
        resp = provider.generate(
            LlmRequest(
                alias="era.deep",
                system=(
                    PromptBlock(
                        text=(
                            "You are a careful senior Python engineer editing a "
                            "production codebase you must not break. You output only "
                            "the complete updated file as raw Python — no prose, no "
                            "markdown fences, no explanation."
                        )
                    ),
                ),
                messages=(
                    LlmMessage.text(
                        "user",
                        f"File: {rel} (part of Era One, a Python voice assistant — "
                        "asyncio + Gemini Live + PyQt6 WebEngine UI).\n\n"
                        f"Defect to fix: {instruction}\n\n"
                        f"Current code:\n{original}\n\n"
                        "Return the COMPLETE updated file. Preserve all existing "
                        "behavior that the change does not touch: comments, public "
                        "function signatures, imports used elsewhere. Make the "
                        "smallest change that fulfils the request. Never remove "
                        "safety checks, confirmation gates, or backups.",
                    ),
                ),
                max_output_tokens=32000,
                timeout_s=600,
                request_id=f"claude_self_improve-{int(time.time())}",
            )
        )
        code = _strip_fences(resp.text)
        if code.strip():
            ledger.record(resp.usage.est_cost_usd, alias="era.deep")
            log("ClaudeSelfImprove", f"generated via Claude CLI: {rel}")
            return code
    except Exception as e:
        log("ClaudeSelfImprove", f"Claude CLI generation failed: {e}")
        if not allow_fallback:
            raise RuntimeError("Claude coding failed; no alternate generator was used") from e

    if not allow_fallback:
        raise RuntimeError("Claude returned no code; no alternate generator was used")
    return _fallback(target, original, instruction, rel)


def _fallback(target: Path, original: str, instruction: str, rel: str) -> str:
    """Same function self_improve._generate_updated_code uses — the default
    generator, so a failed CLI call never breaks the on-disk pipeline."""
    from core.ai_provider import DEFAULT_SMART_MODEL, generate_text

    log("ClaudeSelfImprove", f"falling back to Gemini generator: {rel}")
    code = generate_text(
        (
            f"File: {rel} (part of Era One, a Python voice assistant — "
            "asyncio + Gemini Live + PyQt6 WebEngine UI).\n\n"
            f"Requested change: {instruction}\n\n"
            f"Current code:\n{original}\n\n"
            "Return the COMPLETE updated file. Output ONLY raw Python code — no "
            "explanation, no markdown fences. Preserve all existing behavior that "
            "the change does not touch: comments, public function signatures, "
            "imports used elsewhere. Make the smallest change that fulfils the "
            "request. Never remove safety checks, confirmation gates, or backups."
        ),
        model=DEFAULT_SMART_MODEL,
        system_instruction=(
            "You are a careful senior Python engineer editing a production "
            "codebase you must not break. You output only code."
        ),
    )
    return _strip_fences(code)


def claude_generate_factory(*, allow_fallback: bool = True) -> Any:
    """Return a generate= callable for self_improve._apply.

    The returned function has the signature
        generate(target: Path, original: str, instruction: str) -> str
    and routes through the tool-less Claude CLI, with a Gemini fallback.
    """

    def claude_generate(target: Path, original: str, instruction: str) -> str:
        if allow_fallback:
            return _generate_via_claude_cli(target, original, instruction)
        return _generate_via_claude_cli(target, original, instruction, allow_fallback=False)

    return claude_generate
