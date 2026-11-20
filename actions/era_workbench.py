"""One explicit tool surface for Era's additional desktop and creation capabilities."""

from __future__ import annotations

import json

from core.cancel_registry import CancelledTask, raise_if_cancelled
from core.safety_guard import workbench_action


def era_workbench(parameters: dict) -> dict:
    try:
        raise_if_cancelled()
        arguments = parameters.get("arguments", "{}")
        if not isinstance(arguments, str) or len(arguments) > 2_000_000:
            raise ValueError("arguments must be a JSON object string under 2 MB")
        arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must decode to an object")
        capability = parameters.get("capability")
        action = workbench_action(capability, parameters.get("action", ""))
        if capability == "office":
            from actions.office_architect import run

            return run(action, arguments)
        if capability == "desktop":
            from actions.desktop_orchestrator import run

            return run(action, arguments)
        if capability == "skills":
            from actions.skill_crucible import run

            return run(action, arguments)
        if capability == "reverse_engineering":
            from actions.reverse_inspect import run

            if action in {"javascript", "managed", "wasm", "artifact", "decompile"}:
                from actions.rea_analysis import run as rea_run

                return rea_run(action, arguments)
            if action != "inspect":
                raise ValueError(
                    "Use inspect. Reconstruction requires a separate autonomous_development request with the inspection evidence."
                )
            return run(arguments.get("target", ""))
        if capability == "geography":
            from actions.world_tools import geography

            return geography(action, arguments)
        if capability == "smart_home":
            from actions.world_tools import smart_home

            return smart_home(action, arguments)
        if capability == "circuit":
            from actions.world_tools import circuit

            if action != "guide":
                raise ValueError("Use guide")
            return circuit(arguments)
        if capability == "web_api":
            from actions.api_anything import run

            return run(action, arguments)
        raise ValueError("Unknown capability")
    except CancelledTask:
        raise
    except ImportError as exc:
        return {
            "success": False,
            "error_type": "DependencyMissing",
            "message": f"Optional dependency missing: {exc.name}. Check Settings → Capabilities.",
        }
    except Exception as exc:
        from core.logger import redact_secrets

        return {
            "success": False,
            "error_type": type(exc).__name__,
            "message": redact_secrets(str(exc))[:1000],
        }
