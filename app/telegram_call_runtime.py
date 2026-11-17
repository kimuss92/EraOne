"""Startup/control boundary. Native calling remains gated by unfinished policies."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from app.hud.events import hud
from core.telegram_call_audio import CallPCM
from core.telegram_call_coordinator import CallCoordinator
from core.telegram_call_gemini import LiveSession, call_config


async def handle_tool(engine: Any, args: dict):
    """Model-facing control of the same owner-bound service used by the HUD."""
    from core.tool_result import ToolResult

    service = getattr(engine, "_telegram_calls", None)
    if service is None:
        return ToolResult.fail(
            "Telegram voice calling is unavailable while the call service is not running.",
            error_type="CallUnavailable",
        )
    action = args.get("action", "status")
    if action == "status":
        snapshot = service.snapshot()
        return ToolResult.ok(message=f"Telegram call state: {snapshot['state']}.", data=snapshot)
    try:
        if action == "dial":
            outcome = service.request_call_owner()
            return ToolResult.ok(
                message="Telegram voice call to the configured owner requested; it is not connected yet.",
                data={**outcome, "state": service.snapshot()["state"]},
            )
        if action == "hangup":
            if not service.hangup(service.generation):
                return ToolResult.fail(
                    "No active Telegram call to hang up.", error_type="NoActiveCall"
                )
        elif action == "listen":
            service.begin("listen")
            await save_receiving_preference(service, True)
        elif action == "stop_listening":
            service.stop_listening()
            await save_receiving_preference(service, False)
        else:
            return ToolResult.fail("Unsupported Telegram call action.", error_type="InvalidAction")
        return ToolResult.ok(
            message=f"Telegram call control {action} applied.", data=service.snapshot()
        )
    except PermissionError:
        return ToolResult.fail(
            "Telegram voice calls are not enabled or the owner account is unavailable. Check the Telegram Calls panel.",
            error_type="CallUnavailable",
        )
    except RuntimeError:
        return ToolResult.fail(
            "A Telegram call or its cleanup is already running.", error_type="CallBusy"
        )


async def create_service() -> CallCoordinator:
    from core.protected_config import ProtectedConfigError
    from core.telegram_call_setup import load_settings, private_directory

    setup_error = ""
    try:
        settings = await asyncio.to_thread(lambda: load_settings(private_directory()))
        verified = all(
            settings.get(key) is True
            for key in ("authenticated", "account_authenticated", "owner_bound")
        )
        owner = settings.get("owner_user_id") if verified else None
        if type(owner) is not int or owner <= 0:
            owner = None
        from core.telegram_call_activation import calling_enabled

        enabled = calling_enabled(settings)
    except ProtectedConfigError:
        owner, enabled = None, False
        setup_error = "credentials_unreadable"
    except Exception:
        owner, enabled = None, False
    if owner is None:
        service = CallCoordinator(
            None,
            enabled=False,
            provider=None,
            dispatch=None,
            input_policy=None,
            notify=hud.telegram_call_observed,
        )
        service.error = setup_error
        return service
    from core.telegram_call_tools import RemoteTools, declarations

    actor = RemoteTools(owner)
    provider_active = False

    @asynccontextmanager
    async def provider() -> AsyncIterator[LiveSession]:
        nonlocal provider_active
        from google import genai

        from app.era_live.session_runtime import live_model, managed_live_connection
        from core.utils import get_api_keys
        from core.voice_mode import gemini_voice_name

        try:
            prompt = (Path(__file__).resolve().parents[1] / "core/prompt.txt").read_text(
                encoding="utf-8"
            )
            prompt += "\nYou are speaking on a native Telegram call with your verified owner. Use only the declared tools. Local PC speech and this call are separate conversations. Summarize tool results naturally; never read raw search records aloud. A tool returning busy, unverified, blocked or failed is not done. Crew, coding mode and local voice/standby settings are unavailable in this call; say so if requested. Never approve on the user's behalf. High-risk actions require the authenticated Telegram approval button."
            prompt += "\nThis is a natural phone conversation. Greet the user briefly and warmly, without saying 'system online' or reading system status. Respond to intelligible speech, including short greetings and follow-ups, without requiring a wake word. Keep replies conversational and allow interruptions. Do not repeat your greeting on every turn."
            prompt += "\nExplicit declarations 'I am leaving home', 'I am back home', 'je quitte la maison', and 'je suis rentré' are handled by the local presence controller. Acknowledge them briefly without choosing a tool or requiring GPS sharing. Away mode lasts eight hours; returning home blocks automatic notification calls. Leaving alone does not place a call."
            model = live_model("")
            prompt += "\nWhen the user asks to hang up or end this phone call, or says a short closing goodbye such as okay goodbye or bye, use end_telegram_call immediately. Never tell them to repeat a fixed command. Do not interpret stopping music, quoting a goodbye, or explaining hangup as a request to end the call."
            call_tools = declarations() + [
                {
                    "name": "end_telegram_call",
                    "description": "End this active Telegram call when the caller asks to hang up/end the call now or closes the conversation with bye/goodbye.",
                    "parameters": {"type": "OBJECT", "properties": {}},
                }
            ]
            config = call_config(prompt, gemini_voice_name(), call_tools)
            keys = get_api_keys()[:3]
            if not keys:
                raise RuntimeError("No Gemini call credentials configured.")
            for index, api_key in enumerate(keys):
                client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
                async with AsyncExitStack() as stack:
                    try:
                        session = await stack.enter_async_context(
                            managed_live_connection(client, model=model, config=config)
                        )
                    except Exception as exc:
                        # Use configured backups only before dialing/answering.
                        # Never replay an active conversation or its tool requests.
                        if getattr(exc, "code", None) in {
                            429,
                            500,
                            502,
                            503,
                            504,
                        } and index + 1 < len(keys):
                            continue
                        raise
                    provider_active = True
                    yield session
                    return
        finally:
            provider_active = False

    def input_policy(packet: CallPCM) -> list[bytes]:
        if not provider_active:
            raise RuntimeError("Remote input session is not initialized.")
        if (
            packet.sample_rate != 16000
            or not isinstance(packet.data, bytes)
            or not packet.data
            or len(packet.data) % 2
            or len(packet.data) > 3200
        ):
            raise ValueError("Invalid remote input PCM.")
        # The native transport has verified the Telegram peer and generation.
        # Account-bound phone audio must not depend on a PC/glasses voiceprint.
        return [packet.data]

    service = CallCoordinator(
        owner,
        enabled=enabled,
        provider=provider,
        dispatch=actor.dispatch,
        input_policy=input_policy,
        notify=hud.telegram_call_observed,
    )
    if enabled and settings.get("attended_test_mode") is True:
        import time

        service.set_test_expiry(settings["attended_test_until"] - time.time())
    if enabled and settings.get("auto_receive_calls") is True:
        service.begin("listen")
    return service


async def renew_test_window(service: CallCoordinator, generation: str) -> None:
    """Explicit HUD opt-in; revalidate the owner without login or placing a call."""
    async with service.test_window_lock:
        if service.enabled and generation == service.generation:
            return  # A double click must not rewrite credentials or extend time.
        await _save_test_window(service, generation)


async def save_receiving_preference(service: CallCoordinator, enabled: bool) -> None:
    """Persist the owner's listening switch in daily mode; never enable calls."""
    from core.telegram_call_activation import daily_settings
    from core.telegram_call_setup import load_settings, private_directory, save_settings

    async with service.test_window_lock:
        settings = await asyncio.to_thread(lambda: load_settings(private_directory()))
        if settings.get("daily_calls_opt_in") is not True:
            return
        if settings.get("owner_user_id") != service.owner_id:
            raise PermissionError("Call account binding changed.")
        daily_settings(settings, True)  # Verify the independent owner binding.
        await asyncio.to_thread(
            lambda: save_settings(private_directory(), {**settings, "auto_receive_calls": enabled})
        )


async def _save_test_window(service: CallCoordinator, generation: str) -> None:
    import time

    from core.telegram_call_activation import test_settings
    from core.telegram_call_setup import load_settings, private_directory, save_settings

    if generation != service.generation or not service.snapshot()["canRenewTest"]:
        raise PermissionError("Call testing cannot be enabled in this state.")
    settings = await asyncio.to_thread(lambda: load_settings(private_directory()))
    if settings.get("owner_user_id") != service.owner_id:
        raise PermissionError("Call account binding changed.")
    updated = test_settings(settings, True)
    if generation != service.generation or not service.snapshot()["canRenewTest"]:
        raise PermissionError("Call state changed during activation.")
    await asyncio.to_thread(lambda: save_settings(private_directory(), updated))
    service.enable_test_window(updated["attended_test_until"] - time.time())


def request_control(engine: Any, action: str, generation: str) -> bool:
    """Resolve the current engine per click; marshal commands to its owning loop."""
    loop = getattr(engine, "_loop", None)
    service = getattr(engine, "_telegram_calls", None)
    if (
        action not in {"dial", "listen", "hangup", "recover", "renew_test", "stop_listening"}
        or service is None
        or loop is None
        or not loop.is_running()
    ):
        return False

    def apply() -> None:
        def persist_receiving(enabled: bool) -> None:
            async def persist() -> None:
                try:
                    await save_receiving_preference(service, enabled)
                except Exception:
                    hud.error(
                        "Could not save the incoming-call preference. The current session setting still applies.",
                        where="telegram_call",
                    )

            asyncio.create_task(persist())

        try:
            if action == "stop_listening":
                service.stop_listening()
                persist_receiving(False)
            elif action == "renew_test":

                async def renew() -> None:
                    try:
                        await renew_test_window(service, generation)
                    except Exception:
                        hud.error(
                            "Could not enable call testing. Check account setup or wait for cleanup.",
                            where="telegram_call",
                        )

                asyncio.create_task(renew())
            elif action == "recover":
                if not service.recover(generation):
                    hud.error(
                        "Call cleanup is unavailable or already running.",
                        where="telegram_call",
                    )
            elif action == "hangup":
                if not service.hangup(generation):
                    hud.error("That Telegram call is no longer active.", where="telegram_call")
            else:
                if action == "dial":
                    service.request_call_owner()
                else:
                    service.begin(action)
                    if action == "listen":
                        persist_receiving(True)
        except PermissionError:
            hud.error(
                "Telegram calls are not enabled yet; integration and live checks are pending.",
                where="telegram_call",
            )
        except RuntimeError:
            hud.error(
                "A Telegram call or its cleanup is already running.",
                where="telegram_call",
            )
        except Exception:
            hud.error("Telegram call control failed.", where="telegram_call")

    loop.call_soon_threadsafe(apply)
    return True
