import asyncio
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agent.error_handler import ErrorDecision, analyze_error, generate_fix
from agent.planner import create_plan, replan
from agent.types import TaskPriority

# Correct-SDK wrappers (genai.Client under the hood). The old genai.configure()/
# GenerativeModel() API does NOT exist on google-genai 2.x and raised
# "module 'google.genai' has no attribute 'configure'" — breaking agent_task.
from core.ai_provider import generate_text
from core.confirmation import ConfirmationManager
from core.confirmation_preview import (
    ANNOUNCED_KEY,
    CONFIRM_META,
    PREVIEW_KEY,
    build_confirmation_preview,
    carried_from,
    strip_carried,
)
from core.logger import executor_logger, log_event
from core.pending_confirmation import (
    BUSY_CONFIRMATION_MESSAGE,
    PendingConfirmationBusy,
    get_shared_store,
)
from core.safety_guard import classify_action
from core.self_healing_router import route_tool_call
from core.tool_result import ToolResult
from core.verification import verify_tool_result

# Shared with EraLive so confirmations created on either path are mutually
# resolvable (previously two independent stores caused confirm RuntimeErrors).
_pending_store = get_shared_store()


def _run_generated_code(description: str, speak: Callable | None = None) -> str:
    if os.environ.get("ERA_ALLOW_GENERATED_CODE", "").lower() not in ("1", "true", "yes"):
        raise RuntimeError(
            "Generated code execution is disabled. Use code_helper/dev_agent or set ERA_ALLOW_GENERATED_CODE=1 intentionally."
        )

    if speak:
        speak("Writing custom code for this task, sir.")

    home = Path.home()
    desktop = home / "Desktop"
    downloads = home / "Downloads"
    documents = home / "Documents"

    if not desktop.exists():
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            desktop = Path(winreg.QueryValueEx(key, "Desktop")[0])
        except Exception:
            pass

    code_system_instruction = (
        "You are an expert Python developer. "
        "Write clean, complete, working Python code. "
        "Use standard library + common packages. "
        "Install missing packages with subprocess + pip if needed. "
        "Return ONLY the Python code. No explanation, no markdown, no backticks.\n\n"
        f"SYSTEM PATHS:\n"
        f"  Desktop   = r'{desktop}'\n"
        f"  Downloads = r'{downloads}'\n"
        f"  Documents = r'{documents}'\n"
        f"  Home      = r'{home}'\n"
    )

    try:
        code = generate_text(
            f"Write Python code to accomplish this task:\n\n{description}",
            model="gemini-2.5-flash",
            system_instruction=code_system_instruction,
        ).strip()
        code = re.sub(r"```(?:python)?", "", code).strip().rstrip("`").strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        print(f"[Executor] 🐍 Running generated code: {tmp_path}")

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path.home()),
        )

        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        output = result.stdout.strip()
        error = result.stderr.strip()

        if result.returncode == 0 and output:
            return output
        elif result.returncode == 0:
            return "Task completed successfully."
        elif error:
            raise RuntimeError(f"Code error: {error[:400]}")
        return "Completed."

    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Generated code timed out after 120 seconds.") from e
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Generated code failed: {e}") from e


async def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    if not step_results:
        return params

    params = dict(params)

    if tool == "file_controller" and params.get("action") in ("write", "create_file"):
        content = params.get("content", "")
        if not content or len(content) < 50:
            all_results = [
                v
                for v in step_results.values()
                if v and len(v) > 100 and v not in ("Done.", "Completed.")
            ]
            if all_results:
                combined = "\n\n---\n\n".join(all_results)
                translated = await asyncio.to_thread(_translate_to_goal_language, combined, goal)
                params["content"] = translated
                print("[Executor] 💉 Injected + translated content")

    return params


def _detect_language(text: str) -> str:
    try:
        return (
            generate_text(
                f"What language is this text written in? "
                f"Reply with ONLY the language name in English (e.g. Turkish, English, French).\n\n"
                f"Text: {text[:200]}",
                model="gemini-2.5-flash-lite",
            ).strip()
            or "English"
        )
    except Exception:
        return "English"


def _translate_to_goal_language(content: str, goal: str) -> str:
    if not goal:
        return content
    try:
        target_lang = _detect_language(goal)
        print(f"[Executor] 🌐 Translating to: {target_lang}")

        prompt = (
            f"You are a professional translator. "
            f"Translate the following text into {target_lang}.\n"
            f"IMPORTANT:\n"
            f"- Translate EVERYTHING, leave nothing in English\n"
            f"- Keep all facts, numbers, and data intact\n"
            f"- Keep the structure and formatting\n"
            f"- Output ONLY the translated text, nothing else\n\n"
            f"Text to translate:\n{content[:4000]}"
        )
        translated = generate_text(prompt, model="gemini-2.5-flash").strip()
        print(f"[Executor] ✅ Translation done ({target_lang})")
        return translated
    except Exception as e:
        print(f"[Executor] ⚠️ Translation failed: {e}")
        return content


def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:
    result = _call_tool_result(tool, parameters, speak)
    return result.message or ("Done." if result.success else "Action failed.")


def _announce_pending_plan(preview_text: str, speak) -> bool:
    """Put the resolved plan in front of the user without going through the model.

    The executor has no UI handle, so the durable copy goes to the executor log
    — a file the model cannot edit — and the spoken copy is wrapped in
    `_say_exactly` so it is read verbatim rather than paraphrased. Without
    this the plan reached the user only as `suggested_confirmation_question`,
    a hint to the model the threat model treats as adversarial.
    """
    told = False
    lines = [ln.strip() for ln in preview_text.splitlines() if ln.strip()]
    try:
        log_event(
            executor_logger,
            "tool.confirmation.plan_shown",
            "INFO",
            steps=lines,
        )
        for line in lines:
            print(f"[Executor] CONFIRM: {line}")
        told = True
    except Exception as e:
        print(f"[Executor] ⚠️ could not log the plan for confirmation: {e}")

    if speak:
        try:
            from app.text_intents import _sanitize_spoken, _say_exactly

            spoken = _sanitize_spoken(" ... ".join(lines))
            if spoken:
                speak(_say_exactly(spoken))
                told = True
        except Exception as e:
            print(f"[Executor] ⚠️ could not speak the plan for confirmation: {e}")
    return told


def _call_tool_result(
    tool: str,
    parameters: dict,
    speak: Callable | None,
    confirmed: bool = False,
    auto_confirm: bool = False,
    _confirmed_once: bool = False,
) -> ToolResult:
    start = time.perf_counter()
    original_tool = tool
    original_parameters = dict(parameters or {})

    # ── Routing ──
    try:
        tool, parameters = route_tool_call(tool, parameters or {})
        if (tool, parameters) != (original_tool, original_parameters):
            print(
                f"[Executor] 🧭 routed {original_tool} {original_parameters} → {tool} {parameters}"
            )
    except Exception as route_error:
        print(f"[Executor] ⚠️ routing layer skipped: {route_error}")

    # A caller (i.e. the model) must not be able to supply its own
    # "already approved" plan, so any carried key is dropped before this call
    # is classified, stored or run. Only a preview builder may set one.
    #
    # `_confirmed_once` is set ONLY by confirm_pending_confirmation's internal
    # re-dispatch, whose parameters came straight out of the pending store — so
    # they legitimately carry the approved plan. Stripping them there deleted it
    # and sent the call back to the planner, which is the whole bug this layer
    # exists to prevent, on the normal route a spoken "yes" takes.
    if not _confirmed_once:
        parameters = strip_carried(parameters)

    # ── Safety classification ──
    safety = classify_action(tool, parameters)
    risk_level = safety["risk_level"]
    requires_confirmation = safety["requires_confirmation"]
    reason = safety["reason"]
    print(f"[Executor] 🛡️ safety: {risk_level} — {reason}")
    log_event(
        executor_logger,
        "tool.safety.classified",
        "INFO",
        tool=tool,
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        reason=reason,
    )

    if risk_level == "BLOCKED":
        return ToolResult.fail(
            message="Blocked unsafe action.",
            error=reason,
            error_type="BlockedAction",
            tool_name=tool,
            action_name=parameters.get("action"),
        )

    if requires_confirmation:
        print(f"[Executor] ⚠️ confirmation required ({risk_level})")
        confirmed = bool(confirmed or parameters.get("confirmed", False))
        confirmation_id = parameters.get("confirmation_id", "")

        if confirmed and confirmation_id:
            if not _confirmed_once:
                pending = _pending_store.confirm(confirmation_id)
                if pending is None:
                    return ToolResult.fail(
                        message="Invalid or expired confirmation.",
                        error="Confirmation ID not found or has expired.",
                        error_type="ConfirmationFailed",
                        tool_name=tool,
                        action_name=parameters.get("action"),
                    )
                stored_params = {
                    k: v for k, v in pending.parameters.items() if k not in CONFIRM_META
                }
                call_params = {k: v for k, v in parameters.items() if k not in CONFIRM_META}
                # The approved plan comes from the STORE, never from the
                # caller's echo: it is what the user was actually shown.
                parameters = {**parameters, **carried_from(pending.parameters)}
                if pending.tool != tool or stored_params != call_params:
                    _pending_store.cancel(confirmation_id)
                    return ToolResult.fail(
                        message="Confirmation mismatch.",
                        error="The confirmed action does not match the pending action.",
                        error_type="ConfirmationFailed",
                        tool_name=tool,
                        action_name=parameters.get("action"),
                    )
        elif confirmed and not confirmation_id:
            return ToolResult.fail(
                message="Confirmation rejected: missing confirmation_id.",
                error="Confirmed=True requires a valid confirmation_id.",
                error_type="ConfirmationFailed",
                tool_name=tool,
                action_name=parameters.get("action"),
            )
        else:
            expired = _pending_store.expire_all()
            if expired:
                print(f"[Executor] 🧹 expired {expired} stale pending confirmation(s)")
            # Resolve what this call will actually DO before asking about it,
            # and store that resolution so approval covers the real effects.
            preview_text, carried = build_confirmation_preview(tool, parameters)
            announced = bool(preview_text) and _announce_pending_plan(preview_text, speak)
            try:
                cid = _pending_store.store(
                    tool=tool,
                    parameters={**parameters, **carried},
                    risk_level=risk_level,
                    reason=reason,
                )
            except PendingConfirmationBusy as busy:
                # Never displace the question the user is mid-answer to.
                print(f"[Executor] refused: {busy}")
                return ToolResult.fail(
                    message=BUSY_CONFIRMATION_MESSAGE,
                    error=str(busy),
                    error_type="ConfirmationBusy",
                    tool_name=tool,
                    action_name=parameters.get("action"),
                )
            print(f"[Executor] ⏳ pending confirmation: {tool} id={cid}")
            log_event(
                executor_logger,
                "tool.confirmation.created",
                "INFO",
                tool=tool,
                risk_level=risk_level,
                confirmation_id=cid,
            )
            return ToolResult.fail(
                message="Confirmation required before executing this action.",
                error=reason,
                error_type="ConfirmationRequired",
                tool_name=tool,
                action_name=parameters.get("action"),
                confirmation_id=cid,
                data={
                    **dict(parameters),
                    PREVIEW_KEY: preview_text,
                    ANNOUNCED_KEY: announced,
                },
            )

        ConfirmationManager().request_confirmation(
            tool=tool,
            action_name=parameters.get("action", ""),
            message=reason,
            risk_level=risk_level,
            details=parameters,
            speak=speak,
            confirmed=True,
            auto_confirm=False,
        )

    # ── Tool dispatch ──
    try:
        if tool == "enter_standby":
            raw = "Standby mode is not available in executor context."

        elif tool == "save_memory":
            # Executor path: same audit-only policy as main.py.
            # No hard block — LLM-initiated saves are allowed with audit logging.
            from memory.memory_audit import audit_event
            from memory.memory_manager import update_memory
            from memory.transient_filter import is_transient_memory_key

            category = parameters.get("category", "notes")
            key = parameters.get("key", "")
            value = parameters.get("value", "")
            if is_transient_memory_key(category, key):
                audit_event(
                    operation="block",
                    category=category,
                    key=key,
                    new_value_summary=str(value)[:120] if value else "",
                    source_tool="executor",
                    reason="transient_key",
                    allowed=False,
                )
                raw = "Runtime media state updated."
            elif key and value:
                update_memory({category: {key: {"value": value}}})
                raw = "ok"
            else:
                raw = "ok"

        elif tool == "retrieve_memory_context":
            query = parameters.get("query", "")
            if not query:
                raise RuntimeError("No query provided.")
            from memory.memory_manager import compact_memory_preview, load_memory

            memory = load_memory()
            result = compact_memory_preview(memory, query, top_k=5)
            raw = result if result else "No relevant memories found."

        elif tool == "list_memory_keys":
            from memory.memory_manager import list_memory_keys

            raw = list_memory_keys(
                category=parameters.get("category"),
                full=parameters.get("full", False),
                limit=parameters.get("limit", 50),
                offset=parameters.get("offset", 0),
                show_counts=parameters.get("show_counts", True),
            )

        elif tool == "forget_memory":
            category = parameters.get("category", "")
            key = parameters.get("key", "")
            if not category or not key:
                raise RuntimeError("Both category and key are required.")
            from memory.memory_manager import forget

            raw = forget(key, category)

        elif tool == "open_app":
            from actions.open_app import open_app

            raw = open_app(parameters=parameters, player=None) or "Done."

        elif tool == "web_search":
            from actions.web_search import web_search

            raw = web_search(parameters=parameters, player=None) or "Done."

        elif tool == "game_updater":
            from actions.game_updater import game_updater

            raw = game_updater(parameters=parameters, player=None, speak=speak) or "Done."

        elif tool == "browser_control":
            from actions.browser_control import browser_control

            raw = browser_control(parameters=parameters, player=None) or "Done."

        elif tool == "file_controller":
            from actions.file_controller import file_controller

            raw = file_controller(parameters=parameters, player=None) or "Done."

        elif tool == "code_helper":
            from actions.code_helper import code_helper

            raw = code_helper(parameters=parameters, player=None, speak=speak) or "Done."

        elif tool == "dev_agent":
            from actions.dev_agent import dev_agent

            raw = dev_agent(parameters=parameters, player=None, speak=speak) or "Done."

        elif tool == "screen_process":
            from actions.screen_processor import screen_process

            raw = (
                screen_process(parameters=parameters, player=None, speak=speak)
                or "I could not analyze the screen."
            )

        elif tool == "send_message":
            from actions.send_message import send_message

            raw = send_message(parameters=parameters, player=None) or "Done."

        elif tool == "reminder":
            from actions.reminder import reminder

            raw = reminder(parameters=parameters, player=None) or "Done."

        elif tool == "youtube_video":
            from actions.youtube_video import youtube_video

            raw = youtube_video(parameters=parameters, player=None) or "Done."

        elif tool == "weather_report":
            from actions.weather_report import weather_action

            raw = weather_action(parameters=parameters, player=None) or "Done."

        elif tool == "computer_settings":
            from actions.computer_settings import computer_settings

            raw = computer_settings(parameters=parameters, player=None) or "Done."

        elif tool == "system_performance":
            from actions.system_performance import system_performance

            raw = system_performance(parameters=parameters, player=None) or "Done."

        elif tool == "desktop_control":
            from actions.desktop import desktop_control

            raw = desktop_control(parameters=parameters, player=None) or "Done."

        elif tool == "computer_control":
            from actions.computer_control import computer_control

            raw = computer_control(parameters=parameters, player=None) or "Done."

        elif tool == "generated_code":
            description = parameters.get("description", "")
            if not description:
                raise ValueError("generated_code requires a 'description' parameter.")
            raw = _run_generated_code(description, speak=speak)

        elif tool == "flight_finder":
            from actions.flight_finder import flight_finder

            raw = flight_finder(parameters=parameters, player=None, speak=speak) or "Done."

        elif tool == "confirm_pending_confirmation":
            confirmation_id = parameters.get("confirmation_id", "")
            # allow_sole: the store is single-slot, so there is never ambiguity
            # about *which* action is being confirmed. Without it, a spoken "yes"
            # whose id the model omitted or invented was rejected outright — the
            # live path (main.py) already passes it; this path did not, which is
            # what produced the `confirm_pending_confirmation` RuntimeErrors.
            pending = _pending_store.confirm(confirmation_id, allow_sole=True)
            if pending is None:
                raise RuntimeError("There is no pending action to confirm.")
            stored_params = dict(pending.parameters)
            stored_params["confirmed"] = True
            stored_params["confirmation_id"] = pending.confirmation_id
            print(
                f"[Executor] ✅ pending confirmation accepted: {pending.tool} id={pending.confirmation_id}"
            )
            log_event(
                executor_logger,
                "tool.confirmation.accepted",
                "INFO",
                tool=pending.tool,
                confirmation_id=pending.confirmation_id,
            )
            return _call_tool_result(pending.tool, stored_params, speak, _confirmed_once=True)

        elif tool == "cancel_pending_confirmation":
            if _pending_store.is_pending():
                _pending_store.clear()
                print("[Executor] ❌ pending confirmation cancelled")
                log_event(executor_logger, "tool.confirmation.cancelled", "INFO")
                raw = "Pending action cancelled."
            else:
                print("[Executor] ℹ️ no pending confirmation to cancel")
                log_event(
                    executor_logger, "tool.confirmation.cancelled", "INFO", reason="no pending"
                )
                raw = "There is no pending action to cancel."

        elif tool == "agent_task":
            try:
                from agent.task_queue import get_queue

                priority_map = {
                    "low": TaskPriority.LOW,
                    "normal": TaskPriority.NORMAL,
                    "high": TaskPriority.HIGH,
                }
                priority = priority_map.get(
                    parameters.get("priority", "normal").lower(), TaskPriority.NORMAL
                )
                task_id = get_queue().submit(
                    goal=parameters.get("goal", ""), priority=priority, speak=speak
                )
                raw = f"Task started (ID: {task_id})."
            except Exception as e:
                raw = f"Cannot create agent task in executor context: {e}"

        elif tool == "file_processor":
            try:
                from actions.file_processor import file_processor

                raw = file_processor(parameters=parameters, player=None, speak=speak) or "Done."
            except Exception as e:
                raw = f"File processor unavailable: {e}"

        elif tool == "netflix_control":
            try:
                from actions.netflix_control import netflix_control

                raw = netflix_control(parameters=parameters, player=None) or "Done."
            except Exception as e:
                raw = f"Netflix control unavailable: {e}"

        elif tool == "spotify_control":
            try:
                from actions.spotify_control import spotify_control

                raw = spotify_control(parameters=parameters, player=None) or "Done."
            except Exception as e:
                raw = f"Spotify control unavailable: {e}"

        elif tool == "media_coordinator":
            try:
                from actions.media_coordinator import media_coordinator

                raw = media_coordinator(parameters=parameters, player=None) or "Done."
            except Exception as e:
                raw = f"Media coordinator unavailable: {e}"

        elif tool == "window_control":
            try:
                from actions.window_control import window_control

                raw = window_control(parameters=parameters, player=None, speak=speak) or "Done."
            except Exception as e:
                raw = f"Window control unavailable: {e}"

        elif tool == "premiere_control":
            try:
                from actions.premiere_control import premiere_control

                raw = premiere_control(parameters=parameters, player=None) or "Done."
            except Exception as e:
                raw = f"Premiere control unavailable: {e}"

        elif tool == "agent_browser":
            try:
                from actions.agent_browser_use import agent_browser

                raw = agent_browser(parameters=parameters, player=None) or "Done."
            except Exception as e:
                raw = f"Agent browser unavailable: {e}"

        else:
            print(f"[Executor] ⚠️ Unknown tool '{tool}'")
            return ToolResult.fail(
                message=f"Unknown tool: {tool}",
                error=f"No handler registered for tool '{tool}'.",
                error_type="UnknownTool",
                tool_name=tool,
                action_name=parameters.get("action"),
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        print(f"[Executor] ⏱️ {tool} completed in {duration_ms}ms")
        log_event(
            executor_logger, "tool.execution.completed", "INFO", tool=tool, duration_ms=duration_ms
        )

        result = ToolResult.from_any(
            raw,
            tool_name=tool,
            action_name=parameters.get("action"),
            duration_ms=duration_ms,
        )

    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        print(
            f"[Executor] ❌ {tool} failed after {duration_ms}ms: {type(e).__name__}: {str(e)[:200]}"
        )
        log_event(
            executor_logger,
            "tool.execution.failed",
            "ERROR",
            tool=tool,
            duration_ms=duration_ms,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return ToolResult.fail(
            message=str(e),
            error=str(e),
            error_type=type(e).__name__,
            tool_name=tool,
            action_name=parameters.get("action"),
            duration_ms=duration_ms,
        )

    # ── Attach risk metadata ──
    result.risk_level = risk_level
    result.requires_confirmation = requires_confirmation

    # ── Verification pass ──
    result = verify_tool_result(result, tool=tool, parameters=parameters)

    if not result.success:
        print(f"[Executor] ❌ {tool} failed: {result.error_type} — {result.short_message(200)}")
        log_event(
            executor_logger,
            "tool.execution.failed",
            "ERROR",
            tool=tool,
            error_type=result.error_type,
            error=result.short_message(200),
            duration_ms=result.duration_ms,
        )

    return result


async def _jit_before_mutation(tool: str, action: str) -> None:
    """Capture a fresh screen frame before a mutation action.

    Failures (API errors, missing hardware, or import exceptions) are
    silently swallowed to guarantee the planner's loop never halts.
    """
    try:
        from core.vision_context import jit_vision_capture

        await jit_vision_capture(tool, action)
    except Exception:
        pass


class AgentExecutor:
    MAX_REPLAN_ATTEMPTS = 2

    async def execute(
        self,
        goal: str,
        speak: Callable | None = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:
        print(f"\n[Executor] 🎯 Goal: {goal}")

        replan_attempts = 0
        completed_steps = []
        step_results = {}
        plan = await asyncio.to_thread(create_plan, goal)

        while True:
            steps = plan.get("steps", [])

            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak:
                    speak(msg)
                return msg

            success = True
            failed_step = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak:
                        speak("Task cancelled, sir.")
                    return "Task cancelled."

                step_num = step.get("step", "?")
                tool = step.get("tool", "generated_code")
                desc = step.get("description", "")
                params = step.get("parameters", {})

                params = await _inject_context(params, tool, step_results, goal=goal)

                print(f"\n[Executor] ▶️ Step {step_num}: [{tool}] {desc}")

                # ── JIT vision capture before mutation actions ──
                _jit_action = (params or {}).get("action", "")
                if isinstance(_jit_action, str) and _jit_action:
                    await _jit_before_mutation(tool, _jit_action.lower().strip())

                attempt = 1
                step_ok = False

                while attempt <= 3:
                    if cancel_flag and cancel_flag.is_set():
                        break
                    try:
                        result_obj = await asyncio.to_thread(_call_tool_result, tool, params, speak)
                        step_results[step_num] = result_obj.message

                        # Verification status display
                        v_status = result_obj.verification_status
                        v_icon = (
                            "🔍"
                            if v_status == "VERIFIED"
                            else "⚠️"
                            if v_status == "NOT_VERIFIED"
                            else "❌"
                        )
                        print(
                            f"[Executor] ✅ Step {step_num} done ({v_icon} {v_status}): {result_obj.short_message(100)}"
                        )

                        if result_obj.success:
                            completed_steps.append(step)
                            step_ok = True
                            break
                        else:
                            raise RuntimeError(
                                result_obj.error or result_obj.message or "Tool failed"
                            )

                    except Exception as e:
                        error_msg = str(e)
                        print(
                            f"[Executor] ❌ Step {step_num} attempt {attempt} failed: {error_msg}"
                        )

                        recovery = await asyncio.to_thread(
                            analyze_error, step, error_msg, attempt=attempt
                        )
                        decision = recovery["decision"]
                        user_msg = recovery.get("user_message", "")

                        if speak and user_msg:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            attempt += 1
                            await asyncio.sleep(2)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️ Skipping step {step_num}")
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted, sir. {recovery.get('reason', '')}"
                            if speak:
                                speak(msg)
                            return msg

                        else:
                            fix_suggestion = recovery.get("fix_suggestion", "")
                            if fix_suggestion and tool != "generated_code":
                                try:
                                    fixed_step = await asyncio.to_thread(
                                        generate_fix, step, error_msg, fix_suggestion
                                    )
                                    if speak:
                                        speak("Trying an alternative approach, sir.")
                                    res_obj = await asyncio.to_thread(
                                        _call_tool_result,
                                        fixed_step["tool"],
                                        fixed_step["parameters"],
                                        speak,
                                    )
                                    if res_obj.success:
                                        step_results[step_num] = res_obj.message
                                        completed_steps.append(step)
                                        step_ok = True
                                        break
                                    else:
                                        raise RuntimeError(
                                            res_obj.error or res_obj.message or "Fix failed"
                                        )
                                except Exception as fix_err:
                                    print(f"[Executor] ⚠️ Fix failed: {fix_err}")

                            failed_step = step
                            failed_error = error_msg
                            success = False
                            break

                if not step_ok and not failed_step:
                    failed_step = step
                    failed_error = "Max retries exceeded"
                    success = False

                if not success:
                    break

            if success:
                return await asyncio.to_thread(self._summarize, goal, completed_steps, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = f"Task failed after {replan_attempts} replan attempts, sir."
                if speak:
                    speak(msg)
                return msg

            if speak:
                speak("Adjusting my approach, sir.")

            replan_attempts += 1
            plan = await asyncio.to_thread(replan, goal, completed_steps, failed_step, failed_error)

    def _summarize(self, goal: str, completed_steps: list, speak: Callable | None) -> str:
        fallback = f"All done, sir. Completed {len(completed_steps)} steps for: {goal[:60]}."
        try:
            steps_str = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
            prompt = (
                f'User goal: "{goal}"\n'
                f"Completed steps:\n{steps_str}\n\n"
                "Write a single natural sentence summarizing what was accomplished. "
                "Address the user as 'sir'. Be direct and positive."
            )
            summary = generate_text(prompt, model="gemini-2.5-flash-lite").strip()
            if not summary:
                return fallback
            if speak:
                speak(summary)
            return summary
        except Exception:
            if speak:
                speak(fallback)
            return fallback
