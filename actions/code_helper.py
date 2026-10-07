import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from core.ai_provider import GeminiModel, quota_message
from core.utils import get_base_dir, get_desktop

BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DESKTOP = get_desktop()
MAX_BUILD_ATTEMPTS = 3
GEMINI_MODEL = "gemini-3.8-flash"


def get_api_key() -> str:
    with open(API_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_gemini(model: str = GEMINI_MODEL):
    # genai.configure()/GenerativeModel() were removed in google-genai 2.x;
    # every code_helper call died on AttributeError. ai_provider.GeminiModel
    # is the same .generate_content().text interface on genai.Client.
    return GeminiModel(model)


def _is_era_source(file_path: str) -> bool:
    """True when file_path lives inside Era's own repo (BASE_DIR)."""
    if not file_path:
        return False
    try:
        p = Path(file_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.resolve().relative_to(BASE_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py",
        "py": ".py",
        "javascript": ".js",
        "js": ".js",
        "typescript": ".ts",
        "ts": ".ts",
        "html": ".html",
        "css": ".css",
        "java": ".java",
        "cpp": ".cpp",
        "c": ".c",
        "bash": ".sh",
        "shell": ".sh",
        "powershell": ".ps1",
        "sql": ".sql",
        "json": ".json",
        "rust": ".rs",
        "go": ".go",
    }
    if output_path:
        p = Path(output_path)
        return _repair_glued_path(p if p.is_absolute() else DESKTOP / p)
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"era_one_code{ext}"


def _repair_glued_path(p: Path) -> Path:
    """Undo a space the model typed where a separator belongs.

    "C:\\Users\\me\\Desktop notes.txt" parses as a file named "Desktop notes.txt"
    in C:\\Users\\me — a real, writable spot, so nothing fails and the user never
    finds the file (2026-09-28). When the first word of the name is an existing
    sibling folder and the glued file does not already exist, the space was a
    separator: write into that folder instead."""
    head, sep, tail = p.name.partition(" ")
    if not sep or not tail or p.exists():
        return p
    folder = p.parent / head
    return folder / tail if folder.is_dir() else p


def _read_file(file_path: str) -> tuple[str, str]:
    if not file_path:
        return "", "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return "", f"File not found: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Could not read file: {e}"


def _save_file(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Saved to: {path}"
    except Exception as e:
        return f"Could not save: {e}"


def _preview(code: str, lines: int = 10) -> str:
    all_lines = code.splitlines()
    preview = "\n".join(all_lines[:lines])
    suffix = f"\n... ({len(all_lines) - lines} more lines)" if len(all_lines) > lines else ""
    return preview + suffix


def _has_error(output: str) -> bool:
    error_signals = [
        "error",
        "exception",
        "traceback",
        "syntaxerror",
        "nameerror",
        "typeerror",
        "stderr",
        "failed",
        "crash",
    ]
    return any(s in output.lower() for s in error_signals)


def _take_screenshot() -> Path | None:
    try:
        import pyautogui

        screenshot_path = Path.home() / "Desktop" / f"era_one_debug_{int(time.time())}.png"
        screenshot = pyautogui.screenshot()
        screenshot.save(str(screenshot_path))
        print(f"[Code] 📸 Screenshot: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print(f"[Code] ⚠️ Screenshot failed: {e}")
        return None


def _image_to_base64(path: Path) -> str:
    import base64

    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _kw(desc: str, keywords: list[str]) -> bool:
    """Whole-word keyword match. Substring matching is what once turned a
    description mentioning `run_connect_loop` into action=run and launched a
    second Era instance (Telegram 409, 30s timeout)."""
    return any(re.search(rf"(?<![\w_]){re.escape(k)}(?![\w_])", desc) for k in keywords)


def _detect_intent(description: str, file_path: str, code: str) -> str:
    desc = (description or "").lower()

    screen_kw = [
        "ekrandaki",
        "screen",
        "ekranda",
        "bu hatayı",
        "why am i getting",
        "neden hata",
        "what's wrong",
        "ne yanlış",
        "screenshot",
        "görüntü",
    ]
    if _kw(desc, screen_kw):
        return "screen_debug"

    optimize_kw = [
        "optimize",
        "refactor",
        "clean up",
        "improve",
        "temizle",
        "iyileştir",
        "daha iyi",
        "make it better",
        "hızlandır",
    ]
    if _kw(desc, optimize_kw) and (code or file_path):
        return "optimize"

    if file_path:
        p = Path(file_path)
        edit_kw = [
            "edit",
            "update",
            "modify",
            "change",
            "add",
            "remove",
            "refactor",
            "fix",
            "rename",
            "replace",
            "düzenle",
            "değiştir",
        ]
        run_kw = ["run", "execute", "launch", "start", "çalıştır"]
        build_kw = ["build", "make it work", "try", "attempt"]

        if p.exists() and _kw(desc, edit_kw):
            return "edit"
        if p.exists() and _kw(desc, run_kw):
            return "run"
        if _kw(desc, build_kw):
            return "build"
        if p.exists():
            return "explain"

    explain_kw = ["explain", "what does", "describe", "analyze", "açıkla", "ne yapıyor"]
    if _kw(desc, explain_kw) and (code or file_path):
        return "explain"

    build_kw = ["build", "make it work", "try and", "attempt"]
    if _kw(desc, build_kw):
        return "build"

    return "write"


def _write(description: str, language: str, output_path: str, player=None) -> tuple[str, Path]:
    lang = language or "python"
    model = _get_gemini()

    prompt = f"""You are an expert {lang} developer.
Write clean, working, well-commented {lang} code for the description below.

Rules:
- Output ONLY the code. No explanation, no markdown, no backticks.
- Add helpful inline comments.
- Handle errors and edge cases properly.
- Use modern best practices.

Description: {description}

Code:"""

    response = model.generate_content(prompt)
    code = _clean_code(response.text)
    path = _resolve_save_path(output_path, lang)
    _save_file(path, code)
    return code, path


def _fix_code(code: str, error_output: str, description: str) -> str:
    model = _get_gemini()
    prompt = f"""You are an expert debugger.
The code below failed with the following error. Fix it.
Return ONLY the corrected code — no explanation, no markdown, no backticks.

Original goal: {description}

Error:
{error_output[:2000]}

Broken code:
{code}

Fixed code:"""

    response = model.generate_content(prompt)
    return _clean_code(response.text)


def _run_file(path: Path, args: list, timeout: int) -> str:
    interpreters = {
        ".py": [sys.executable],
        ".js": ["node"],
        ".ts": ["ts-node"],
        ".sh": ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb": ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}."

    # Unbuffered so every line the script prints reaches the pipe as it happens.
    # Without this, a script killed by the timeout loses its buffered output and
    # its side effects (deleted/written files) go unreported.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(path.parent),
            env=env,
        )
        output = result.stdout.strip()
        error = result.stderr.strip()
        parts = []
        if output:
            parts.append(f"Output:\n{output}")
        if error:
            parts.append(f"Stderr:\n{error}")
        return "\n\n".join(parts) if parts else "Executed with no output."

    except subprocess.TimeoutExpired as e:
        # A timeout stops the process; it does not undo what it already did.
        # Report the output captured up to the kill so the partial effects are visible.
        def _text(v) -> str:
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
            return (v or "").strip()

        partial = _text(e.stdout)
        partial_err = _text(e.stderr)
        parts = [
            f"Timed out after {timeout}s: the script was stopped before it finished. "
            "Any changes it made before the stop (files deleted, moved or written) are "
            "real and were NOT rolled back — check the filesystem before reporting the outcome."
        ]
        if partial:
            parts.append(f"Output before the stop:\n{partial[-4000:]}")
        if partial_err:
            parts.append(f"Stderr before the stop:\n{partial_err[-2000:]}")
        if not partial and not partial_err:
            parts.append("The script printed nothing before the stop.")
        return "\n\n".join(parts)
    except FileNotFoundError:
        return f"Interpreter not found: {interp[0]}."
    except Exception as e:
        return f"Execution error: {e}"


def _build(description, language, output_path, args, timeout, speak=None, player=None) -> str:
    if not description:
        return "Please describe what you want me to build, sir."

    if player:
        player.write_log("[Code] Build started...")

    lang = language or "python"

    try:
        code, path = _write(description, lang, output_path, player)
        print(f"[Code] ✅ Written: {path}")
    except Exception as e:
        msg = f"Could not write initial code: {e}"
        if speak:
            speak(msg)
        return msg

    last_output = ""
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        print(f"[Code] 🔄 Attempt {attempt}/{MAX_BUILD_ATTEMPTS}")
        if player:
            player.write_log(f"[Code] Attempt {attempt}...")

        last_output = _run_file(path, args, timeout)

        if not _has_error(last_output):
            msg = (
                f"Build complete, sir. "
                f"The code is working after {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to {path}."
            )
            if speak:
                speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        print(f"[Code] ⚠️ Error on attempt {attempt}, fixing...")
        if player:
            player.write_log(f"[Code] Fixing (attempt {attempt})...")

        try:
            code = _fix_code(code, last_output, description)
            _save_file(path, code)
        except Exception as e:
            msg = (
                quota_message(e, what="Code fixing")
                or f"Could not fix code on attempt {attempt}: {e}"
            )
            if speak:
                speak(msg)
            return msg

    msg = (
        f"I was unable to build a working version after {MAX_BUILD_ATTEMPTS} attempts, sir. "
        f"The last error was: {last_output[:200]}"
    )
    if speak:
        speak(msg)
    return f"{msg}\n\nLast code saved to: {path}"


def _save_verbatim(content: str, language: str, output_path: str) -> str:
    """Save content the caller already wrote — no generation step."""
    path = _resolve_save_path(output_path, language)
    saved = _save_file(path, content)
    if not saved.startswith("Saved to:"):
        return saved
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        return f"Could not save: {path} did not verify on disk after writing."
    return f"{saved}\n\nPreview:\n{_preview(content)}"


def _write_action(description, language, output_path, player, code: str = "") -> str:
    # Content supplied verbatim (a note, a report, a finished script) is saved
    # as-is. Without this branch a write carrying `code` but no `description`
    # answered "please describe..." — which reads as SUCCESS — and the file
    # the user asked for was never created (2026-09-28).
    if code:
        return _save_verbatim(code, language, output_path)
    if not description:
        return "Could not write: no description or content was given, sir."
    if player:
        player.write_log("[Code] Writing code...")
    try:
        code, path = _write(description, language, output_path, player)
        print(f"[Code] ✅ Written: {path}")
        return f"Code written. Saved to: {path}\n\nPreview:\n{_preview(code)}"
    except Exception as e:
        return quota_message(e, what="Code generation") or f"Could not generate code: {e}"


def _edit_action(file_path, instruction, player) -> str:
    if not file_path:
        return "Please provide a file path to edit, sir."
    if not instruction:
        return "Please describe what change to make, sir."

    content, err = _read_file(file_path)
    if err:
        return err

    # Backup before edit
    p = Path(file_path)
    if p.exists():
        try:
            import shutil as _shutil
            from datetime import datetime

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = p.parent / f"{p.name}.bak-{ts}"
            _shutil.copy2(str(p), str(backup))
            print(f"[Code] Backup created: {backup.name}")
        except Exception as e:
            return f"Backup failed, refusing to edit: {e}"

    if player:
        player.write_log("[Code] Editing file...")

    model = _get_gemini()
    prompt = f"""You are an expert code editor.
Apply the following change to the code below.
Return ONLY the complete updated code — no explanation, no markdown, no backticks.

Change: {instruction}

Original code:
{content}

Updated code:"""

    try:
        response = model.generate_content(prompt)
        edited = _clean_code(response.text)
    except Exception as e:
        return f"Could not edit code: {e}"

    status = _save_file(Path(file_path), edited)
    print(f"[Code] ✅ Edited: {file_path}")
    return f"File edited. {status}\n\nPreview:\n{_preview(edited)}"


def _explain_action(file_path, code, player) -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to explain, sir."

    if player:
        player.write_log("[Code] Analyzing code...")

    model = _get_gemini()
    prompt = f"""Explain what this code does in simple, clear language.
Focus on: what it does, how it works, and any important details.
Be concise — 3 to 6 sentences maximum.

Code:
{code[:4000]}

Explanation:"""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return quota_message(e, what="Code explanation") or f"Could not explain code: {e}"


def _run_action(file_path, args, timeout, player) -> str:
    if not file_path:
        return "Please provide a file path to run, sir."
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"
    if player:
        player.write_log(f"[Code] Running {p.name}...")
    return _run_file(p, args, timeout)


def _optimize_action(file_path, code, language, output_path, player) -> str:

    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to optimize, sir."

    if player:
        player.write_log("[Code] Optimizing code...")

    lang = language or "python"
    model = _get_gemini()

    prompt = f"""You are an expert {lang} developer and code reviewer.
Optimize the following code for:
1. Performance — eliminate unnecessary operations, use efficient data structures
2. Readability — clear variable names, proper formatting, logical structure
3. Best practices — modern {lang} patterns, error handling, type hints if applicable
4. Remove dead code, redundant comments, and unnecessary complexity

Return ONLY the optimized code — no explanation, no markdown, no backticks.

Original code:
{code[:6000]}

Optimized code:"""

    try:
        response = model.generate_content(prompt)
        optimized = _clean_code(response.text)
    except Exception as e:
        return f"Could not optimize code: {e}"

    # Kaydet
    if file_path:
        save_path = Path(file_path)
    else:
        save_path = _resolve_save_path(output_path, lang)

    status = _save_file(save_path, optimized)
    print(f"[Code] ✅ Optimized: {save_path}")

    original_lines = len(code.splitlines())
    optimized_lines = len(optimized.splitlines())
    diff = original_lines - optimized_lines

    return (
        f"Code optimized. {status}\n"
        f"Lines: {original_lines} → {optimized_lines} "
        f"({'−' if diff > 0 else '+'}{abs(diff)} lines)\n\n"
        f"Preview:\n{_preview(optimized)}"
    )


def _screen_debug_action(description, file_path, player, speak=None) -> str:

    if player:
        player.write_log("[Code] Taking screenshot for analysis...")

    print("[Code] 📸 Capturing screen for debug...")

    screenshot_path = _take_screenshot()
    if not screenshot_path:
        return "Could not take screenshot, sir. Please make sure PyAutoGUI is installed."

    file_content = ""
    if file_path:
        file_content, err = _read_file(file_path)
        if err:
            print(f"[Code] ⚠️ Could not read file: {err}")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=get_api_key())

        image_bytes = screenshot_path.read_bytes()
        _image_to_base64(screenshot_path)

        user_question = (
            description or "What error or problem do you see on the screen? How can it be fixed?"
        )

        context = ""
        if file_content:
            context = f"\n\nAdditionally, here is the related file content:\n```\n{file_content[:4000]}\n```"

        analysis_prompt = f"""You are an expert programmer and debugger analyzing a screenshot.

User's question: {user_question}{context}

Please:
1. Identify any errors, exceptions, or problems visible on the screen
2. Explain what is causing the problem in simple terms
3. Provide a concrete fix or solution
4. If there's code visible, show the corrected version

Be specific and actionable. If you see an error message, quote it exactly."""

        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            analysis_prompt,
        ]

        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
        )

        analysis = response.text.strip()
        print("[Code] ✅ Screen analysis complete")

        try:
            screenshot_path.unlink()
        except Exception:
            pass

        if file_path and file_content:
            code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", analysis, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip()
                save_path = Path(file_path)
                _save_file(save_path, fixed_code)
                analysis += f"\n\n✅ Fixed code has been saved to: {file_path}"
                print(f"[Code] ✅ Fixed code saved: {file_path}")

        return analysis

    except Exception as e:
        try:
            screenshot_path.unlink()
        except Exception:
            pass
        return f"Screen analysis failed: {e}"


def code_helper(
    parameters: dict, response=None, player=None, session_memory=None, speak=None
) -> str:
    """
    Called from main.py.

    parameters:
        action      : write | edit | explain | run | build | screen_debug | optimize | auto
        description : What the code should do / what change to make / what problem to analyze
        language    : Programming language (default: python)
        output_path : Where to save — user specifies full path or filename
        file_path   : Path to existing file (edit / explain / run / build / optimize)
        code        : Raw code string (explain/optimize without a file)
        args        : CLI argument list for run/build
        timeout     : Execution timeout in seconds (default: 30)
    """
    p = parameters or {}
    action = p.get("action", "auto").lower().strip()
    description = p.get("description", "").strip()
    language = p.get("language", "python").strip()
    output_path = p.get("output_path", "").strip()
    file_path = p.get("file_path", "").strip()
    code = p.get("code", "").strip()
    args = p.get("args", [])
    timeout = int(p.get("timeout", 30))

    if os.environ.get("ERA_ALLOW_GENERATED_CODE", "").lower() not in ("1", "true", "yes"):
        return "Code execution is disabled. Set ERA_ALLOW_GENERATED_CODE=1 to enable code_helper."

    if action == "auto":
        action = _detect_intent(description, file_path, code)
        print(f"[Code] 🤖 Auto-detected: {action}")

    # Era's own source is off-limits here: editing it unverified can brick her,
    # and RUNNING main.py spawns a second Era instance (Telegram 409 conflict).
    # Only read-only explain is allowed; everything else goes through the
    # backup+verify+rollback pipeline in actions/self_improve.py.
    if action != "explain" and _is_era_source(file_path):
        return (
            f"{file_path} is part of my own code — I don't {action} my own source "
            "through code_helper. Use the self_improve tool instead: it backs up, "
            "verifies, and rolls back automatically."
        )

    if action == "write":
        return _write_action(description, language, output_path, player, code=code)

    elif action == "edit":
        return _edit_action(file_path, description or p.get("instruction", ""), player)

    elif action == "explain":
        return _explain_action(file_path, code, player)

    elif action == "run":
        return _run_action(file_path, args, timeout, player)

    elif action == "build":
        return _build(description, language, output_path, args, timeout, speak, player)

    elif action == "optimize":
        return _optimize_action(file_path, code, language, output_path, player)

    elif action == "screen_debug":
        return _screen_debug_action(description, file_path, player, speak)

    else:
        return f"Unknown action: '{action}'. Use write, edit, explain, run, build, optimize, or screen_debug."
