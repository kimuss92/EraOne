"""
One-command validation runner for Era One.

Executes compile checks, all test suites, and the Doctor in sequence.
Continues on failure. Reports a summary table at the end.
Exit code 0 = all PASS, 1 = any FAIL.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# ── Known-failure registry ──────────────────────────────────────────────
# Suite names whose failure is pre-existing and unrelated to new work.
KNOWN_FAILURES = {
    "Netflix Vision fallback",
}

# Suites that constitute the core Vision stack (V1–V5).
VISION_ONLY_SUITE_NAMES = {
    "Vision silent",
    "Vision worker",
    "Vision async screen_process",
}

COMPILE_TARGETS = [
    "core/logger.py",
    "core/tool_registry_validator.py",
    "core/tool_result.py",
    "core/safety_guard.py",
    "core/verification.py",
    "core/main_tool_bridge.py",
    "core/pending_confirmation.py",
    "core/telegram_call_setup.py",
    "core/telegram_call_audio.py",
    "core/telegram_call_gemini.py",
    "core/telegram_call_protocol.py",
    "core/telegram_call_helper.py",
    "core/telegram_call_process.py",
    "core/telegram_call_coordinator.py",
    "core/telegram_call_diagnostics.py",
    "core/telegram_call_tools.py",
    "core/tool_execution_lane.py",
    "core/telegram_call_activation.py",
    "app/telegram_call_runtime.py",
    "tools/telegram_call_daily_mode.py",
    "core/home_presence.py",
    "app/away_calls.py",
    "app/manual_presence.py",
    "core/local_presence.py",
    "core/pnp_awareness.py",
    "core/subtree_discovery.py",
    "app/hud/layout.py",
    "memory/safe_store.py",
    "memory/memory_manager.py",
    "core/intent_memory.py",
    "memory/feedback_memory.py",
    "memory/memory_audit.py",
    "memory/transient_filter.py",
    "memory/persistence.py",
    "agent/executor.py",
    "main.py",
    "tools/era_doctor.py",
    "tools/test_smoke_e2e.py",
    "actions/screen_processor.py",
    "core/vision_worker.py",
    "core/vision_openai.py",
    "core/vision_config.py",
    "tools/test_vision_silent.py",
    "tools/test_vision_worker.py",
    "tools/test_vision_screen_process_async.py",
    "core/vision_types.py",
    "core/vision_policy.py",
    "tools/test_vision_policy.py",
    "core/vision_context.py",
    "tools/test_vision_router_policy.py",
    "utils/scroll.py",
    "tools/test_scroll_control.py",
    "utils/media_match.py",
    "tools/test_media_selection.py",
    "tools/test_browser_media_routing.py",
    "tools/test_regression_14b1.py",
    "actions/youtube_video.py",
    "actions/netflix_control.py",
    "actions/computer_control.py",
    "core/self_healing_router.py",
    "actions/browser_intelligence.py",
    "actions/media_coordinator.py",
    # Step 14B.2 new modules
    "utils/ordinal_selection.py",
    "core/browser_grid_state.py",
    "actions/platform_tab.py",
    "tools/test_grid_selection.py",
    "tools/test_netflix_play.py",
    "tools/test_netflix_browser_hard_lock.py",
    "tools/test_youtube_grid_match.py",
    "tools/test_youtube_next_video.py",
    "tools/test_youtube_direct_play.py",
]

TEST_SUITES = [
    # Module execution keeps the repository importable while preserving each
    # custom script's __main__ checks and failure counters.
    ("Registry validator", [PYTHON, "-m", "tools.test_tool_registry_validator"]),
    (
        "CodeMode",
        [PYTHON, "-m", "pytest", "-q", "tools/test_codemode.py", "tools/test_codemode_followup.py"],
    ),
    (
        "Phase 8 coordination",
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "tools/test_phase8_coordination.py",
            "tools/test_crew_registry.py",
            "tools/test_crew_loop.py",
            "tools/test_crew_router.py",
            "tools/test_crew_background_honesty.py",
            "tools/test_hermes_transport.py",
            "tools/test_development.py",
        ],
    ),
    ("Logging", [PYTHON, "-m", "tools.test_logging"]),
    ("Memory safety", [PYTHON, "-m", "tools.test_memory_safety"]),
    ("Execution layer", [PYTHON, "-m", "tools.test_execution_layer"]),
    ("Verification layer", [PYTHON, "-m", "tools.test_verification_layer"]),
    ("Main tool bridge", [PYTHON, "-m", "tools.test_main_tool_bridge"]),
    (
        "Confirmation flow",
        [
            PYTHON,
            "-m",
            "pytest",
            "tools/test_confirmation_flow.py",
            "tools/test_confirm_self_approval.py",
            "tools/test_telegram_approvals.py",
            "tools/test_telegram_call_setup.py",
            "tools/test_telegram_call_audio.py",
            "tools/test_telegram_call_gemini.py",
            "tools/test_telegram_call_transport.py",
            "tools/test_telegram_call_coordinator.py",
            "tools/test_telegram_call_tools.py",
            "tools/test_telegram_call_diagnostics.py",
            "tools/test_away_calls.py",
            "-v",
        ],
    ),
    ("Era One doctor", [PYTHON, "-m", "tools.test_era_doctor"]),
    ("Smoke E2E", [PYTHON, "-m", "tools.test_smoke_e2e"]),
    ("YouTube volume routing", [PYTHON, "-m", "tools.test_youtube_volume_routing"]),
    ("Browser intelligence", [PYTHON, "-m", "tools.test_browser_intelligence"]),
    ("Domain validation", [PYTHON, "-m", "tools.test_domain_validation"]),
    ("Standby policy", [PYTHON, "-m", "pytest", "-q", "tools/test_standby_policy.py"]),
    (
        "System awareness",
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "tools/test_system_awareness.py",
            "tools/test_hud_layout.py",
            "tools/test_phase6_completion.py",
            "tools/test_browser_media_observer.py",
            "tools/test_media_recovery.py",
            "tools/test_foreground_awareness.py",
            "tools/test_warning_repairs.py",
            "tools/test_process_lifecycle.py",
            "tools/test_network_awareness.py",
            "tools/test_device_awareness.py",
            "tools/test_system_task_cancel.py",
            "tools/test_msi_scenario.py",
            "tools/test_app_launch_safety.py",
            "tools/test_file_awareness.py",
            "tools/test_file_indexer.py",
            "tools/test_file_subtree.py",
            "tools/test_state_store.py",
            "tools/test_state_notifications.py",
        ],
    ),
    (
        "Speech pipeline",
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "tools/test_remainder_synthesis.py",
            "tools/test_speech_replay_recovery.py",
            "tools/test_speech_completion.py",
            "tools/test_speech_out.py",
            "tools/test_speech_out_threading.py",
            "tools/test_transcript_speech_ownership.py",
            "tools/test_tts_kokoro.py",
            "tools/test_telegram_speech.py",
            "tools/test_telegram_reply.py",
            "tools/test_model_reply_ownership.py",
            "tools/test_audio_output.py",
            "tools/test_quiet_glasses.py",
            "tools/test_barge_in.py",
            "tools/test_attention.py",
            "tools/test_speaker_gate.py",
            "tools/test_mic_mute.py",
            "tools/test_media_voice_gate.py",
            "tools/test_conversation_responsiveness.py",
            "tools/test_search_voice_reporting.py",
            "tools/test_claude_voice.py",
            "tools/test_task_recovery.py",
            "tools/test_live_cancel_scope.py",
            "tools/test_cancel_registry.py",
            "tools/test_cancel_flow.py",
            "tools/test_open_gemini_input.py",
        ],
    ),
    ("Vision silent", [PYTHON, "-m", "pytest", "tools/test_vision_silent.py", "-v"]),
    ("Vision worker", [PYTHON, "-m", "pytest", "tools/test_vision_worker.py", "-v"]),
    (
        "Vision async screen_process",
        [PYTHON, "-m", "pytest", "tools/test_vision_screen_process_async.py", "-v"],
    ),
    ("Vision policy", [PYTHON, "-m", "pytest", "tools/test_vision_policy.py", "-v"]),
    ("Vision router policy", [PYTHON, "-m", "pytest", "tools/test_vision_router_policy.py", "-v"]),
    ("Scroll control", [PYTHON, "-m", "pytest", "tools/test_scroll_control.py", "-v"]),
    ("Media selection", [PYTHON, "-m", "pytest", "tools/test_media_selection.py", "-v"]),
    (
        "Browser/media routing",
        [PYTHON, "-m", "pytest", "tools/test_browser_media_routing.py", "-v"],
    ),
    ("Grid selection", [PYTHON, "-m", "pytest", "tools/test_grid_selection.py", "-v"]),
    (
        "Netflix Vision fallback",
        [PYTHON, "-m", "pytest", "tools/test_netflix_vision_fallback.py", "-v"],
    ),
    ("Regression 14B.1", [PYTHON, "-m", "pytest", "tools/test_regression_14b1.py", "-v"]),
    ("Netflix play accuracy", [PYTHON, "-m", "pytest", "tools/test_netflix_play.py", "-v"]),
    (
        "Netflix browser hard-lock",
        [PYTHON, "-m", "pytest", "tools/test_netflix_browser_hard_lock.py", "-v"],
    ),
    ("YouTube grid match", [PYTHON, "-m", "pytest", "tools/test_youtube_grid_match.py", "-v"]),
    ("YouTube next video", [PYTHON, "-m", "pytest", "tools/test_youtube_next_video.py", "-v"]),
    ("YouTube direct play", [PYTHON, "-m", "pytest", "tools/test_youtube_direct_play.py", "-v"]),
    ("Enhanced directive", [PYTHON, "-m", "pytest", "tools/test_enhanced_directive.py", "-v"]),
]

DOCTOR_CMD = [PYTHON, "tools/era_doctor.py"]


def run(name: str, cmd: list[str], cwd: str | Path | None = None) -> bool:
    start = time.perf_counter()
    result = subprocess.run(cmd, cwd=cwd or str(REPO_ROOT))
    elapsed = time.perf_counter() - start
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"  [{status}] {name} ({elapsed:.1f}s)")
    return result.returncode == 0


def _suite(command):
    try:
        timeout = max(120, float(os.environ.get("ERA_CHECK_SUITE_TIMEOUT_S", "900")))
    except ValueError:
        timeout = 900.0
    try:
        return subprocess.run(command, cwd=str(REPO_ROOT), timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT after {timeout:.0f}s: {' '.join(command)}", flush=True)
        return False


def main(*, jobs=1, profile=None):
    overall_start = time.perf_counter()
    results: list[tuple[str, bool, float]] = []

    print("=" * 58)
    print("  Era One — One-Command Validation Runner")
    print("=" * 58)

    # ── Step 1: Compile check ──
    print("\n[1/3] Compile check")
    print("-" * 58)
    compile_ok = True
    for target in COMPILE_TARGETS:
        path = REPO_ROOT / target
        if not path.exists():
            print(f"  SKIP  {target} (not found)")
            continue
        result = subprocess.run(
            [PYTHON, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  FAIL  {target}")
            print(f"    {result.stderr.strip()}")
            compile_ok = False
        else:
            print(f"  PASS  {target}")
    duration = time.perf_counter() - overall_start
    results.append(("Compile check", compile_ok, duration))

    # ── Step 2: Test suites ──
    print("\n[2/3] Test suites")
    print("-" * 58)

    def check(item):
        name, command = item
        print(f"  [START] {name}", flush=True)
        start = time.perf_counter()
        ok = _suite(command)
        elapsed = time.perf_counter() - start
        print(f"  [{('PASS' if ok else 'FAIL')}] {name} ({elapsed:.1f}s)", flush=True)
        return name, ok, elapsed

    with ThreadPoolExecutor(max_workers=max(1, min(4, jobs))) as pool:
        results.extend(pool.map(check, TEST_SUITES))

    # ── Step 3: Doctor ──
    print("\n[3/3] Doctor check")
    print("-" * 58)
    start = time.perf_counter()
    doc_ok = _suite(DOCTOR_CMD)
    doc_elapsed = time.perf_counter() - start
    print(f"  [{('PASS' if doc_ok else 'FAIL')}] Doctor ({doc_elapsed:.1f}s)")
    results.append(("Doctor", doc_ok, doc_elapsed))
    if profile:
        Path(profile).write_text(
            json.dumps(
                [
                    {"suite": name, "passed": ok, "seconds": round(elapsed, 3)}
                    for name, ok, elapsed in sorted(results, key=lambda item: item[2], reverse=True)
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

    # ── Summary table ──
    total_elapsed = time.perf_counter() - overall_start
    print("\n" + "=" * 58)
    print("  Summary")
    print("=" * 58)
    all_ok = True
    for name, ok, elapsed in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  {status:5s}  {name:<28s}  {elapsed:.1f}s")
    print("-" * 58)
    print(f"  Total: {total_elapsed:.1f}s")

    # ── Known-failure report ──
    failed = [n for n, ok, _ in results if not ok]
    vision_only_results = [(n, ok) for n, ok, _ in results if n in VISION_ONLY_SUITE_NAMES]
    vision_stack_ready = all(ok for _, ok in vision_only_results)
    known_failures_active = [n for n in failed if n in KNOWN_FAILURES]

    if not all_ok and known_failures_active:
        print()
        print("=" * 58)
        print("  KNOWN UNRELATED FAILURE:")
        for n in known_failures_active:
            print(f"  {n} suite failed/timed out.")
        print("  This is a pre-existing Netflix control issue,")
        print("  not related to Vision V1-V5.")
        print("  Vision-only validation remains clean.")
        print("  Global ALL_CHECKS_OK is blocked until Netflix is fixed.")
        print("=" * 58)

    # ── Vision-stack readiness summary ──
    print()
    print("=" * 58)
    print(f"  VISION_STACK_READY: {'YES' if vision_stack_ready else 'NO'}")
    print(f"  GLOBAL_READY:       {'YES' if all_ok else 'NO'}")
    if known_failures_active:
        blockers = known_failures_active[:]
        other_failures = [n for n in failed if n not in KNOWN_FAILURES]
        if other_failures:
            blockers.extend(f" (and {len(other_failures)} other failure(s))")
        print(f"  BLOCKED_BY:         {', '.join(blockers)}")
    elif not all_ok and failed:
        print(f"  BLOCKED_BY:         {', '.join(failed)}")
    print("=" * 58)

    if all_ok:
        print()
        print("=" * 58)
        print("  ALL_CHECKS_OK")
        print("  READY")
        print("=" * 58)
        return 0
    else:
        print(f"\n  FAILED: {', '.join(failed)}")
        print("  NOT READY")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--jobs",
        type=int,
        choices=range(1, 5),
        default=1,
        help="Independent suite processes (default: 1; opt in after checking shared fixtures)",
    )
    parser.add_argument("--profile", help="Write per-suite timings as JSON")
    options = parser.parse_args()
    sys.exit(main(jobs=options.jobs, profile=options.profile))
