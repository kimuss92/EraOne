#!/usr/bin/env python3
"""Era One Doctor — healthcheck tool. Read-only, no side effects.

Usage:
    python tools/era_doctor.py

Exit code 0 = READY (no ERRORs), 1 = NOT READY (ERRORs found).
"""

import importlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory.safe_store import validate_json_file  # noqa: E402  (needs the sys.path bootstrap above)

RESULTS: list[tuple[str, str, str, str]] = []


def _fail(msg: str) -> tuple[str, str]:
    return "ERROR", msg


def _warn(msg: str) -> tuple[str, str]:
    return "WARN", msg


def _ok(msg: str = "") -> tuple[str, str]:
    return "OK", msg


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),
)


def _looks_like_placeholder(value: str) -> bool:
    text = (value or "").strip().upper()
    return (
        not text
        or "YOUR_" in text
        or text.endswith("_OPTIONAL")
        or text in {"CHANGE_ME", "PLACEHOLDER"}
    )


def _contains_secret_literal(data: dict) -> bool:
    for value in data.values():
        if isinstance(value, str):
            if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
                return True
    return False


def _extract_tool_section(content: str) -> str | None:
    """Extract the TOOL_DECLARATIONS = [...] list content with bracket matching."""
    marker = "TOOL_DECLARATIONS = ["
    start = content.find(marker)
    if start == -1:
        return None
    bracket_start = start + content[start:].find("[")
    depth = 0
    for i in range(bracket_start, len(content)):
        if content[i] == "[":
            depth += 1
        elif content[i] == "]":
            depth -= 1
            if depth == 0:
                return content[bracket_start : i + 1]
    return None


def check_python_version() -> tuple[str, str]:
    # The project targets py312 (ruff.toml), and this module uses syntax
    # that would not import on anything older — so a runtime "too old"
    # branch is unreachable by construction. Just report what is running.
    return _ok(f"{sys.version}")


def check_directories() -> tuple[str, str]:
    required = ["core", "actions", "agent", "memory", "config", "tools"]
    missing = [d for d in required if not (REPO_ROOT / d).is_dir()]
    if not (REPO_ROOT / "main.py").is_file():
        missing.append("main.py")
    if missing:
        return _fail(f"Missing: {', '.join(missing)}")
    return _ok()


def check_core_imports() -> tuple[str, str]:
    modules = [
        "core.tool_result",
        "core.safety_guard",
        "core.confirmation",
        "core.verification",
        "core.action_verifiers",
        "core.action_registry",
    ]
    errors: list[str] = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as e:
            errors.append(f"{mod}: {e}")
    if errors:
        return _fail("; ".join(errors))
    return _ok()


def check_api_key_config() -> tuple[str, str]:
    config_path = REPO_ROOT / "config" / "api_keys.json"
    env_key = os.environ.get("GEMINI_API_KEY", "")

    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return _fail(f"config/api_keys.json is invalid JSON: {e}")
        if not isinstance(data, dict):
            return _fail("config/api_keys.json root is not a JSON object")
        if _contains_secret_literal(data):
            return _warn(
                "config/api_keys.json contains literal credentials; keep this file local and never commit it"
            )
        key = data.get("gemini_api_key", "")
        if isinstance(key, str) and not _looks_like_placeholder(key):
            return _ok("present in local config/api_keys.json")
        if env_key:
            return _ok("present in GEMINI_API_KEY env var")
        return _fail(
            "gemini_api_key missing from config/api_keys.json and GEMINI_API_KEY env var not set"
        )
    else:
        if env_key:
            return _ok("present in GEMINI_API_KEY env var")
        return _fail("config/api_keys.json not found and GEMINI_API_KEY env var not set")


def check_cloudinary_config() -> tuple[str, str]:
    """Cloudinary is the ONLY video/image host (default decided 2026-07-29;
    the bunny/litterbox/catbox fallbacks were removed 2026-07-30). Without
    credentials, publishing cannot stage media at all — so this check being
    red means the publish lane is down, not merely degraded. It only verifies
    that CREDENTIALS resolve somewhere (env or config/media_host.json); it
    never makes a network call, matching every other doctor check's read-only
    contract."""
    try:
        import media_host
    except Exception as e:
        return _warn(f"media_host.py failed to import: {e}")

    cfg = {}
    config_path = REPO_ROOT / "config" / "media_host.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return _fail(f"config/media_host.json is invalid JSON: {e}")
        if _contains_secret_literal(cfg):
            return _warn(
                "config/media_host.json contains literal credentials; keep this "
                "file local and never commit it"
            )

    creds = media_host._cloudinary_creds(cfg)
    if creds:
        return _ok(f"configured — cloud '{creds['cloud_name']}'")
    return _fail(
        "Cloudinary not configured (CLOUDINARY_URL / CLOUDINARY_CLOUD_NAME+"
        "API_KEY+API_SECRET / config/media_host.json) — it is now the only "
        "media host, so publishing cannot stage a reel or image at all"
    )


def check_action_modules() -> tuple[str, str]:
    action_dir = REPO_ROOT / "actions"
    if not action_dir.is_dir():
        return _warn("actions/ directory not found")
    action_files = sorted(f.stem for f in action_dir.glob("*.py") if not f.stem.startswith("_"))
    missing: list[str] = []
    import_errors: list[str] = []
    for name in action_files:
        spec = importlib.util.find_spec(f"actions.{name}")
        if spec is None:
            missing.append(name)
            continue
        try:
            importlib.import_module(f"actions.{name}")
        except Exception:
            import_errors.append(name)
    issues: list[str] = []
    if missing:
        issues.append(f"Missing: {', '.join(missing)}")
    if import_errors:
        issues.append(f"Import issues: {', '.join(import_errors)}")
    if issues:
        return _warn("; ".join(issues))
    return _ok(f"{len(action_files)} modules")


def check_long_term_memory() -> tuple[str, str]:
    path = REPO_ROOT / "memory" / "long_term.json"
    result = validate_json_file(path)
    if result["status"] == "MISSING":
        return _warn("File not found (gitignored, normal for fresh clones)")
    if result["status"] == "CORRUPTED_RECOVERED":
        return _warn(result["message"])
    if result["status"] == "CORRUPTED_NO_BACKUP":
        return _fail(result["message"])
    size_kb = result["file_size"] / 1024
    detail = f"{size_kb:.1f} KB" if size_kb > 0 else "OK"
    return _ok(detail)


def check_feedback_memory() -> tuple[str, str]:
    path = REPO_ROOT / "memory" / "feedback_memory.jsonl"
    if not path.is_file():
        return _warn("File not found")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    invalid_lines: list[str] = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            invalid_lines.append(str(i))
    if invalid_lines:
        return _warn(f"Invalid JSON on lines: {', '.join(invalid_lines)}")
    return _ok(f"{len(lines)} line(s)")


def check_obsidian_vault() -> tuple[str, str]:
    """Is the community-management lane actually connected to the Obsidian vault?

    A missing vault is a WARNING, never a failure: CM jobs still run, they just
    run without the standing strategy notes. But it is silent otherwise — the
    brief simply comes out thinner — so surfacing it here is the only way to
    notice that OneDrive moved the folder.
    """
    try:
        from core import cm_vault
    except ImportError as e:
        return _warn(f"cm_vault not importable: {e}")

    info = cm_vault.doctor()
    if not info["exists"]:
        return _warn(f"Vault not found at {info['vault_dir']} — CM runs lose vault context")
    if not info["notes"]:
        return _warn(f"Vault at {info['vault_dir']} has no notes")
    folders = ", ".join(f"{k} {v}" for k, v in sorted(info["by_folder"].items()))
    brands = ", ".join(info["brands_seen"]) or "none tagged"
    return _ok(f"{info['notes']} note(s) [{folders}] · brands: {brands}")


def check_vault_consistency() -> tuple[str, str]:
    """Is the vault FILED correctly, not merely present?

    check_obsidian_vault above answers "can Era reach it". This answers "is what
    she reads still complete", which is a different question with entirely silent
    failure modes: a note whose `brand:` has a typo is excluded from every brief,
    a note past the per-note cap is truncated mid-sentence in every run, and a
    brand whose notes overflow the budget loses whichever one happens to be last.
    None of it raises, logs, or changes the shape of a brief. This is the check.

    Imports core.vault_audit, NOT core.vault_maintenance: that module contains no
    write path at all, so running the doctor cannot mutate the vault even if the
    repair logic next door is later changed badly. Repairs happen in the
    scheduled pass, never from a diagnostic.
    """
    try:
        from core import vault_audit
    except ImportError as e:
        return _warn(f"vault_audit not importable: {e}")

    info = vault_audit.doctor()
    counts = f"{info['errors']} error(s), {info['warnings']} warning(s)"
    if info["errors"]:
        return _fail(f"{counts} — {info['top'][0] if info['top'] else 'see Era_Vault_Health.md'}")
    if info["warnings"]:
        return _warn(f"{counts} — run `python -m core.vault_maintenance` for detail")
    return _ok("filed correctly; every note fits the brief budget")


def check_daily_log() -> tuple[str, str]:
    """Era's dated day log — the cross-brand view no per-brand run log can give.

    Lives in `01 - Daily Notes/`, which cm_vault walks (so the log is indexed and
    findable) but which is deliberately absent from _BRIEF_FOLDERS, so it costs no
    brief budget.
    """
    try:
        from core import daily_note
    except ImportError as e:
        return _warn(f"daily_note not importable: {e}")

    info = daily_note.doctor()
    if not info["exists"]:
        return _warn(f"no day-log folder at {info['daily_dir']} — CM runs aren't day-logged")
    template = "template present" if info["template"] else "no template (using fallback shape)"
    today = "today logged" if info["today_exists"] else "nothing logged today yet"
    return _ok(f"{template}; {today}")


def check_cm_learning() -> tuple[str, str]:
    """The CM's feedback store. Empty is normal on a fresh install."""
    try:
        from core import cm_learning
    except ImportError as e:
        return _warn(f"cm_learning not importable: {e}")

    info = cm_learning.stats()
    if not info["total"]:
        return _ok("no lessons recorded yet (learns from revise/approve orders)")
    per_brand = ", ".join(f"{k}: {v}" for k, v in sorted(info["brands"].items()))
    return _ok(f"{info['total']} lesson(s) — {per_brand}")


def check_memory_audit_log() -> tuple[str, str]:
    path = REPO_ROOT / "logs" / "memory_audit.jsonl"
    if not path.is_file():
        # Create it if missing — fresh install is OK
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            return _ok("Created (fresh install)")
        except Exception:
            return _warn("File not found and could not create")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    valid = 0
    invalid = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
            valid += 1
        except json.JSONDecodeError:
            invalid += 1
    if invalid:
        return _warn(f"{valid} valid, {invalid} invalid line(s)")
    return _ok(f"{valid} line(s)")


def check_registry_consistency() -> tuple[str, str]:
    try:
        from core.tool_registry_validator import validate_tool_registry

        issues = validate_tool_registry()
    except Exception as e:
        return _warn(f"Registry validator unavailable: {e}")
    errors = [i for i in issues if i["severity"] == "ERROR"]
    warnings = [i for i in issues if i["severity"] == "WARN"]
    if errors:
        details = "; ".join(i["message"] for i in errors)
        return _fail(f"{len(errors)} error(s): {details}")

    warn_parts = []
    for w in warnings:
        cat = w["category"].replace("_", " ")
        tools_part = w["message"].split(": ", 1)[-1]
        tool_count = len(tools_part.split(", ")) if tools_part else 0
        warn_parts.append(f"{tool_count} {cat}")
    summary = f"{len(issues)} issues: 0 errors"
    if warn_parts:
        summary += ", " + ", ".join(warn_parts)
    return _ok(summary)


def check_llm_registry() -> tuple[str, str]:
    """The model alias registry (config/llm.yaml).

    Everything here fails SILENTLY at runtime: a typo in `routing.by_task` does
    not crash, it just resolves to nothing and the request quietly takes the
    fallback. This is the only place such a typo becomes visible.
    """
    try:
        from core.llm import registry
    except ImportError as e:
        return _warn(f"llm registry not importable: {e}")

    info = registry.doctor()
    if not info["loaded"]:
        return _fail(info["error"] or "registry could not be loaded")

    if info["problems"]:
        return _fail(f"{len(info['problems'])} problem(s): " + "; ".join(info["problems"]))

    parts = [f"{len(info['available'])}/{info['aliases']} aliases available"]
    parts.append(f"default {info['default']}, voice {info['voice']}")
    if info["unavailable"]:
        parts.append("unavailable: " + ", ".join(u["alias"] for u in info["unavailable"]))
    if info["daily_budget_usd"] is not None:
        parts.append(f"budget ${info['daily_budget_usd']}/day")
    return _ok(" · ".join(parts))


def check_llm_providers() -> tuple[str, str]:
    """Can the providers the registry names be reached from this machine?

    Local probes only — nothing here spends money or opens a socket. A pass
    means "worth trying", not "verified": Phase 4 established that an
    already-connected interactive connector does NOT carry into a headless run.
    """
    try:
        from core.llm import registry
    except ImportError as e:
        return _warn(f"llm registry not importable: {e}")

    rows = registry.provider_health()
    if not rows:
        return _warn("no providers declared")

    errors = [r for r in rows if r["status"] == "error"]
    warns = [r for r in rows if r["status"] == "warn"]
    if errors:
        return _fail("; ".join(f"{r['provider']}: {r['detail']}" for r in errors))
    if warns:
        return _warn("; ".join(f"{r['provider']}: {r['detail']}" for r in warns))
    return _ok(", ".join(r["provider"] for r in rows) + " reachable")


def check_test_files() -> tuple[str, str]:
    test_files = [
        "tools/test_execution_layer.py",
        "tools/test_verification_layer.py",
        "tools/test_main_tool_bridge.py",
    ]
    syntax_errors: list[str] = []
    import_errors: list[str] = []
    for tf in test_files:
        path = REPO_ROOT / tf
        if not path.is_file():
            syntax_errors.append(f"{tf} not found")
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as e:
            syntax_errors.append(f"{tf}: {e}")
            continue
        mod_name = tf.replace("/", ".").replace(".py", "")
        spec = importlib.util.find_spec(mod_name)
        if spec is None:
            import_errors.append(f"{tf}: module not found in path")
            continue
        try:
            import io

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            mod = importlib.import_module(mod_name)
            sys.stdout = old_stdout
            if mod is None:
                import_errors.append(f"{tf}: import returned None")
        except Exception as e:
            sys.stdout = old_stdout
            import_errors.append(f"{tf}: {e}")
    if syntax_errors:
        return _fail("; ".join(syntax_errors))
    if import_errors:
        return _warn("; ".join(import_errors))
    return _ok()


def check_optional_packages() -> tuple[str, str]:
    packages = [
        "psutil",
        "pywinctl",
        "pyautogui",
        "pyperclip",
        ("PIL", "pillow"),
        "pyaudio",
        "spotipy",
        "python_vlc",
        "playwright",
        "vosk",
        "sounddevice",
    ]
    not_found: list[str] = []
    for pkg in packages:
        if isinstance(pkg, tuple):
            import_name, pip_name = pkg
        else:
            import_name = pip_name = pkg
        spec = importlib.util.find_spec(import_name)
        if spec is None:
            not_found.append(pip_name)
    if not_found:
        return _warn(f"Not installed: {', '.join(not_found)}")
    return _ok()


def check_vosk_model() -> tuple[str, str]:
    spec = importlib.util.find_spec("vosk")
    if spec is None:
        return _warn("vosk package not installed")
    model_dir_name = "vosk-model-small-en-us-0.15"
    model_dir = REPO_ROOT / "models" / model_dir_name
    if not model_dir.is_dir():
        return _warn(f"Model directory 'models/{model_dir_name}' not found")
    key_files = ["am", "conf", "graph", "ivector"]
    missing_files = [f for f in key_files if not (model_dir / f).exists()]
    if missing_files:
        return _warn(f"Model directory found but missing files: {', '.join(missing_files)}")
    return _ok("model present in local ignored models/ directory")


def check_log_folder() -> tuple[str, str]:
    log_dir = REPO_ROOT / "logs"
    if not log_dir.is_dir():
        return _warn("Logs folder not found")
    try:
        files = [f for f in log_dir.iterdir() if f.is_file()]
        count = len(files)
        total_size = sum(f.stat().st_size for f in files)
        if total_size > 100 * 1024 * 1024:
            return _warn(f"Logs folder: {count} file(s), {total_size / 1024 / 1024:.1f} MB total")
        detail = f"{count} file(s)"
        if count > 0:
            main_log = log_dir / "era.log"
            if main_log.is_file():
                detail += f", era.log {main_log.stat().st_size / 1024:.1f} KB"
            else:
                detail += ", era.log not found"
        return _ok(detail)
    except Exception as e:
        return _warn(f"Could not read logs folder: {e}")


def check_root_cleanliness() -> tuple[str, str]:
    """Report zero-byte files in the project root (accidental shell redirects).

    Report only: empty placeholder files are preserved by policy, so this never
    recommends deleting them. Review the list by hand.
    """
    allow = {".gitkeep", "__init__.py", "py.typed", ".gitignore", ".env"}
    try:
        junk = sorted(
            p.name
            for p in REPO_ROOT.iterdir()
            if p.is_file() and p.name not in allow and p.stat().st_size == 0
        )
    except Exception as e:
        return _warn(f"Could not scan root: {e}")
    if not junk:
        return _ok("root clean — no zero-byte junk files")
    preview = ", ".join(junk[:8]) + (" ..." if len(junk) > 8 else "")
    return _warn(
        f"{len(junk)} zero-byte file(s) in root ({preview}); "
        f"empty files are preserved by policy — review by hand, nothing is auto-deleted"
    )


def check_memory_backups() -> tuple[str, str]:
    memory_dir = REPO_ROOT / "memory"
    if not memory_dir.is_dir():
        return _warn("memory/ directory not found")
    new_dir = memory_dir / "backups"
    legacy = sorted(memory_dir.glob("*.jsonbak"))
    new = sorted(new_dir.glob("*.jsonbak")) if new_dir.is_dir() else []
    if not legacy and not new:
        return _ok("No backup files found")
    details = []
    for bk in new:
        try:
            size_kb = bk.stat().st_size / 1024
            details.append(f"backups/{bk.stem} ({size_kb:.1f} KB)")
        except Exception:
            details.append(f"backups/{bk.stem}")
    for bk in legacy:
        try:
            size_kb = bk.stat().st_size / 1024
            details.append(f"{bk.stem} (legacy, {size_kb:.1f} KB)")
        except Exception:
            details.append(f"{bk.stem} (legacy)")
    return _ok(f"{len(new) + len(legacy)} file(s): {', '.join(details)}")


CHECKS: list[tuple[str, str, callable]] = [
    ("Python version", "CRITICAL", check_python_version),
    ("Required directories/files", "CRITICAL", check_directories),
    ("Core module imports", "CRITICAL", check_core_imports),
    ("API key config", "CRITICAL", check_api_key_config),
    ("Cloudinary media host config", "INFO", check_cloudinary_config),
    ("Tool registry consistency", "INFO", check_registry_consistency),
    ("LLM alias registry", "INFO", check_llm_registry),
    ("LLM provider reachability", "INFO", check_llm_providers),
    ("Existing test files", "CRITICAL", check_test_files),
    ("Action module imports", "INFO", check_action_modules),
    ("memory/long_term.json", "INFO", check_long_term_memory),
    ("Memory backups", "INFO", check_memory_backups),
    ("Logs folder", "INFO", check_log_folder),
    ("Root cleanliness", "INFO", check_root_cleanliness),
    ("memory/feedback_memory.jsonl", "INFO", check_feedback_memory),
    ("Obsidian vault (CM context)", "INFO", check_obsidian_vault),
    ("Vault consistency (PARA/brand filing)", "INFO", check_vault_consistency),
    ("Era day log (01 - Daily Notes)", "INFO", check_daily_log),
    ("CM learned feedback", "INFO", check_cm_learning),
    ("logs/memory_audit.jsonl", "INFO", check_memory_audit_log),
    ("Optional packages", "INFO", check_optional_packages),
    ("Vosk model", "INFO", check_vosk_model),
]


def run_checks(repo_root_override: str | Path | None = None) -> list[tuple[str, str, str, str]]:
    global REPO_ROOT
    if repo_root_override is not None:
        REPO_ROOT = Path(repo_root_override)
        path_str = str(REPO_ROOT)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    results: list[tuple[str, str, str, str]] = []
    for name, severity, fn in CHECKS:
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "ERROR", f"Exception: {e}"
        results.append((name, severity, status, detail))
    return results


def print_report(results: list[tuple[str, str, str, str]]) -> tuple[int, int]:
    error_count = 0
    warn_count = 0
    print()
    print(f"{'=' * 60}")
    print("  Era One Doctor — Health Check")
    print(f"{'=' * 60}")
    print()
    for name, _severity, status, detail in results:
        icon = {"OK": "PASS", "WARN": "WARN", "ERROR": "FAIL"}
        s = icon.get(status, "????")
        line = f"  [{s:4s}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        if status == "ERROR":
            error_count += 1
        elif status == "WARN":
            warn_count += 1
    print()
    print(f"{'=' * 60}")
    print(f"  Errors: {error_count}   Warnings: {warn_count}")
    if error_count == 0:
        print("  Status: READY")
    else:
        print("  Status: NOT READY")
    print(f"{'=' * 60}")
    return error_count, warn_count


def main():
    results = run_checks()
    error_count, _ = print_report(results)
    sys.exit(1 if error_count > 0 else 0)


if __name__ == "__main__":
    main()
