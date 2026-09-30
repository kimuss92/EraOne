"""Claude as a reasoning oracle, over the subscription `claude -p` CLI.

WHY A SUBPROCESS AND NOT THE ANTHROPIC SDK
  Chosen 2026-07-30: Era reaches Claude through the subscription CLI, the same
  mechanism era_dispatch.py already uses for the community-management lane. No
  API key, no `anthropic` dependency, no per-request billing surface.

TOOLS ARE DISABLED, DELIBERATELY
  Every invocation passes --allowedTools "" so Claude cannot touch the machine.
  It returns text or JSON; Era's own deterministic layer (classify_action ->
  pending_confirmation -> _execute_tool_raw) executes everything.

  This is a STRONGER guarantee than the API design would have given. There, the
  model emits tool_use blocks and Era's layer decides what may run — safe, but
  it depends on that layer being correct. Here the model has no tool surface at
  all, so a prompt-injected instruction to "delete the file" has nothing to call.
  If a future change enables tools here, tool execution moves inside Claude Code
  and OUTSIDE Era's safety gates; that is a decision to take deliberately, not
  a flag to flip. tools/test_claude_cli_adapter.py asserts the flag stays off.

MEASURED FACTS THIS DESIGN IS BUILT ON (probes, 2026-07-30)
  * Every invocation reloads the Claude Code harness — the prompt cache does
    NOT survive across processes. A trivial one-line prompt costs ~$0.058 and
    ~2.7s on sonnet. That floor, not the marginal token cost, is what makes
    routing to Claude a decision rather than a default.
  * Running from Era's repo costs ~11K extra tokens and 2-5s per call, because
    Claude Code loads this project's CLAUDE.md and skills. Running from an
    empty directory halves cost and latency. Hence the scratch cwd.
  * The prompt must arrive on STDIN. A positional prompt argument is rejected
    ("Input must be provided either through stdin or as a prompt argument"),
    and on Windows CLAUDE_BIN resolves to a .cmd shim anyway.
  * --output-format json returns real telemetry: total_cost_usd, a full token
    breakdown, ttft_ms and duration_ms. Era has never had per-call cost
    accounting for Gemini; it gets it here for free.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any

from core.llm import claude_accounts, registry
from core.llm.types import (
    Capability,
    LlmError,
    LlmRequest,
    LlmResponse,
    StreamEvent,
    Usage,
)

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# api_error_status -> Era's ErrorKind. The CLI reports HTTP status in the JSON
# rather than raising, so this is the primary classification path.
_STATUS_KIND = {
    400: "unsupported",
    401: "auth",
    403: "auth",
    404: "unsupported",  # e.g. a model alias the account cannot reach
    408: "timeout",
    413: "context_overflow",
    429: "rate_limit",
    500: "transient",
    502: "transient",
    503: "transient",
    529: "transient",
}


def classify_cli_result(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Inspect a --output-format json payload. Returns (kind, message) on
    failure, or None when the run succeeded.

    Checked in order of specificity, because the CLI can report success at one
    level and failure at another: `subtype` was "success" on a run whose
    `terminal_reason` was "api_error" and whose `result` carried the 404 text.
    Trusting `subtype` alone would have called that a success and handed the
    error string back as Claude's answer.
    """
    status = payload.get("api_error_status")
    if isinstance(status, int) and status >= 400:
        kind = _STATUS_KIND.get(status, "transient")
        return kind, f"HTTP {status}: {str(payload.get('result') or '')[:300]}"

    if payload.get("terminal_reason") == "api_error":
        return "transient", str(payload.get("result") or "the CLI reported an API error")[:300]

    if payload.get("is_error"):
        return "transient", str(payload.get("result") or "the CLI reported an error")[:300]

    stop = payload.get("stop_reason")
    if stop == "refusal":
        return "refusal", str(payload.get("result") or "the model declined")[:300]
    if stop == "max_tokens":
        # Not an error the caller should retry blindly — the answer is truncated.
        return "invalid_response", "the reply hit the output limit and is truncated"

    return None


def _usage_from(payload: dict[str, Any], started: float) -> Usage:
    """Read the CLI's telemetry. Never raises — a renamed field must cost a
    metric, not the answer that was already produced."""
    u = payload.get("usage") or {}

    def _int(key: str) -> int:
        try:
            return int(u.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        cost = float(payload.get("total_cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0

    def _ms(key: str) -> int | None:
        try:
            value = payload.get(key)
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return Usage(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_read_tokens=_int("cache_read_input_tokens"),
        cache_write_tokens=_int("cache_creation_input_tokens"),
        est_cost_usd=cost,
        # The CLI reports a real figure rather than one derived from prices.
        reported_cost=True,
        latency_ms=_ms("duration_ms") or int((time.perf_counter() - started) * 1000),
        first_token_ms=_ms("ttft_ms"),
    )


class ClaudeCliProvider:
    """LLMProvider over `claude -p`. Reasoning only: no tools, no machine access."""

    name = "claude_cli"

    def __init__(self, *, runner: Any = None) -> None:
        # Injectable so tests drive the adapter without spawning a real CLI (a
        # real run costs money and ~3s). Defaults to the subprocess runner.
        self._run = runner or self._run_subprocess
        self._live: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ── capabilities ────────────────────────────────────────────────────────

    def capabilities(self, alias: str) -> Capability:
        entry = registry.resolve(alias)
        if entry.provider != self.name:
            raise LlmError(
                "unsupported",
                f"{alias} belongs to provider {entry.provider!r}, not {self.name!r}",
                provider=self.name,
                alias=alias,
            )
        return entry.capabilities

    def _entry(self, request: LlmRequest) -> registry.ModelEntry:
        try:
            entry = registry.resolve(request.alias)
        except registry.RegistryError as e:
            raise LlmError("unsupported", str(e), provider=self.name, alias=request.alias) from e
        if entry.provider != self.name:
            raise LlmError(
                "unsupported",
                f"{request.alias} is a {entry.provider} alias, not {self.name}",
                provider=self.name,
                alias=request.alias,
            )
        caps = entry.capabilities
        if not caps.available:
            # The era.cheap case: capable in principle, unreachable here. Say so
            # in words that can be spoken, rather than letting a 404 surface
            # mid-sentence.
            raise LlmError(
                "unsupported",
                caps.unavailable_reason or f"{request.alias} is not available",
                provider=self.name,
                alias=request.alias,
            )
        if request.tools:
            raise LlmError(
                "unsupported",
                "Claude runs tool-free on this path: it returns reasoning, and Era's "
                "own gated layer executes every action. Enabling tools here would move "
                "execution inside Claude Code, outside Era's safety gates.",
                provider=self.name,
                alias=request.alias,
            )
        return entry

    # ── command construction ────────────────────────────────────────────────

    def _build_command(self, entry: registry.ModelEntry) -> list[str]:
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--output-format",
            "json",
            # Empty string, not omitted: omitting it lets Claude Code use its
            # default tool set, which is exactly what must not happen here.
            "--allowedTools",
            "",
        ]
        if entry.model:
            cmd += ["--model", entry.model]
        return cmd

    def _build_env(
        self, entry: registry.ModelEntry, account: claude_accounts.Account | None = None
    ) -> dict[str, str]:
        env = dict(os.environ)
        config_dir = (
            account.config_dir
            if account is not None
            else str(entry.provider_config.get("config_dir") or "").strip()
        )
        if config_dir:
            # Selects WHICH subscription account runs this, the same mechanism
            # era_dispatch uses to keep brand accounts apart.
            env["CLAUDE_CONFIG_DIR"] = config_dir
        return env

    @staticmethod
    def _compose_prompt(request: LlmRequest) -> str:
        """System blocks first, then the turns.

        `claude -p` has no separate system-prompt channel, so the system text
        is prepended rather than passed apart. Block order is preserved because
        it is the caller's caching intent, even though this path cannot honour
        a cache across processes.
        """
        chunks: list[str] = []
        system = request.system_text()
        if system:
            chunks.append(system)
        for message in request.messages:
            if message.role == "system":
                continue
            body = message.as_text
            if not body:
                continue
            chunks.append(body if len(request.messages) == 1 else f"{message.role}: {body}")
        if request.json_schema is not None:
            chunks.append(
                "Respond with a single JSON object and nothing else — no prose, no "
                "markdown fences. It must validate against this JSON Schema:\n"
                + json.dumps(request.json_schema, indent=2)
            )
        return "\n\n".join(chunks).strip()

    # ── the subprocess ──────────────────────────────────────────────────────

    def _run_subprocess(
        self, cmd: list[str], prompt: str, env: dict[str, str], cwd: str, timeout: float, rid: str
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                # Pinned exactly as era_dispatch pins them, and for the reason it
                # records: Claude's output carries accented text, em dashes and
                # emoji. Without this, text=True decodes with the Windows locale
                # codec and a single non-cp1252 byte raises UnicodeDecodeError
                # inside subprocess's reader THREAD — uncaught, that took the
                # whole engine down.
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            raise LlmError(
                "unsupported",
                f"the Claude CLI was not found on PATH as {CLAUDE_BIN!r}. "
                "Set CLAUDE_BIN to its full path.",
                provider=self.name,
            ) from e

        with self._lock:
            self._live[rid] = proc
        try:
            out, err = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            proc.communicate()
            raise LlmError(
                "timeout",
                f"the Claude CLI did not finish within {timeout:.0f}s",
                provider=self.name,
            ) from e
        finally:
            with self._lock:
                self._live.pop(rid, None)
        return proc.returncode, out or "", err or ""

    # ── generate ────────────────────────────────────────────────────────────

    def generate(self, request: LlmRequest) -> LlmResponse:
        entry = self._entry(request)
        prompt = self._compose_prompt(request)
        if not prompt:
            raise LlmError("unsupported", "nothing to ask", provider=self.name, alias=request.alias)

        rid = request.request_id or f"claude-{int(time.time() * 1000)}"

        # ── account failover ────────────────────────────────────────────────
        # One subscription hitting its usage limit used to stop every Claude
        # path in Era. Each account is tried in turn, but ONLY for failures the
        # next account could plausibly survive (out of usage, rate limited,
        # signed out). A refusal or a bad schema fails identically everywhere,
        # so retrying it would just spend another ~3s and ~$0.06 to lose again.
        pool = claude_accounts.usable(entry.provider_config)
        if not pool:
            # No account list configured: behave exactly as before this existed.
            return self._attempt(entry, request, prompt, rid, None)

        last: LlmError | None = None
        for account in pool:
            try:
                response = self._attempt(entry, request, prompt, rid, account)
            except LlmError as exc:
                last = exc
                if not claude_accounts.should_switch(exc.kind, exc.message):
                    raise
                claude_accounts.note_failure(account, f"{exc.kind}: {exc.message}")
                continue
            claude_accounts.note_success(account)
            return response

        # Every account failed the same recoverable way. The last real error is
        # re-raised with the accounts named, rather than replaced by a summary:
        # the reason has to stay the one the CLI actually gave.
        raise LlmError(
            last.kind,
            f"every Claude account failed ({', '.join(a.name for a in pool)}): {last.message}",
            provider=self.name,
            alias=request.alias,
        ) from last

    def _attempt(
        self,
        entry: registry.ModelEntry,
        request: LlmRequest,
        prompt: str,
        rid: str,
        account: claude_accounts.Account | None,
    ) -> LlmResponse:
        """One `claude -p` invocation against one account."""
        started = time.perf_counter()

        # A scratch cwd is not cosmetic: measured, it halves cost and latency by
        # keeping Claude Code from loading Era's CLAUDE.md and skills.
        scratch = None
        cwd = os.getcwd()
        if entry.provider_config.get("use_minimal_cwd", True):
            scratch = tempfile.mkdtemp(prefix="era_llm_")
            cwd = scratch
        try:
            code, out, err = self._run(
                self._build_command(entry),
                prompt,
                self._build_env(entry, account),
                cwd,
                request.timeout_s,
                rid,
            )
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)

        payload = self._parse(out, err, code, request)

        failure = classify_cli_result(payload)
        if failure is not None:
            kind, message = failure
            raise LlmError(kind, message, provider=self.name, alias=request.alias)

        text = str(payload.get("result") or "").strip()
        if request.json_schema is not None:
            text = _strip_fences(text)
            try:
                json.loads(text)
            except (ValueError, TypeError) as e:
                raise LlmError(
                    "invalid_response",
                    f"expected JSON, got: {text[:200]}",
                    provider=self.name,
                    alias=request.alias,
                ) from e

        return LlmResponse(
            request_id=rid,
            provider=self.name,
            model_id=self._resolved_model(payload, entry),
            alias=request.alias,
            text=text,
            stop_reason="end_turn",
            usage=_usage_from(payload, started),
            raw=payload,
        )

    def _parse(self, out: str, err: str, code: int, request: LlmRequest) -> dict[str, Any]:
        text = (out or "").strip()
        if not text:
            raise LlmError(
                "transient",
                f"the Claude CLI produced no output (exit {code}): {(err or '').strip()[:300]}",
                provider=self.name,
                alias=request.alias,
            )
        try:
            payload = json.loads(text)
        except ValueError as e:
            # A non-JSON body usually means the CLI refused to start (bad flag,
            # not logged in) and printed a human message.
            raise LlmError(
                "transient",
                f"the Claude CLI returned non-JSON output (exit {code}): {text[:300]}",
                provider=self.name,
                alias=request.alias,
            ) from e
        if not isinstance(payload, dict):
            raise LlmError(
                "invalid_response",
                f"expected a JSON object from the CLI, got {type(payload).__name__}",
                provider=self.name,
                alias=request.alias,
            )
        return payload

    @staticmethod
    def _resolved_model(payload: dict[str, Any], entry: registry.ModelEntry) -> str:
        """Prefer what actually served the request over what was asked for.

        `--model sonnet` is an alias the CLI resolves itself, and the resolution
        can surprise: on this machine `--model haiku` resolves to a third-party
        model entirely. Reporting the resolved id means a log says which model
        really answered.
        """
        usage = payload.get("modelUsage")
        if isinstance(usage, dict) and usage:
            return str(next(iter(usage)))
        return entry.model

    # ── stream / cancel ─────────────────────────────────────────────────────

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        """`claude -p` returns one complete result, so this yields the whole
        text as a single delta. Chunking a finished string would fabricate a
        first-token latency that a metric would then believe."""
        response = self.generate(request)
        if response.text:
            yield StreamEvent(kind="text_delta", text=response.text)
        yield StreamEvent(kind="usage", usage=response.usage)
        yield StreamEvent(kind="turn_complete", detail={"stop_reason": response.stop_reason})

    def cancel(self, request_id: str) -> None:
        """Terminate an in-flight run. Safe for unknown or finished ids."""
        with self._lock:
            proc = self._live.get(request_id)
        if proc is None:
            return
        try:
            proc.terminate()
        except (OSError, ValueError):
            pass


def _strip_fences(text: str) -> str:
    """Claude wraps JSON in ```json fences even when told not to — same
    treatment core/ai_provider.generate_json applies to Gemini."""
    import re

    return re.sub(r"```(?:json)?", "", text or "").strip().rstrip("`").strip()
