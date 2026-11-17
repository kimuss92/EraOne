"""Exercise the real PowerShell recovery flow against isolated process stubs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).with_name("open_era_one_team.ps1")
SYSTEM_PWSH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PowerShell/7/pwsh.exe"
POWERSHELL = str(SYSTEM_PWSH) if SYSTEM_PWSH.is_file() else shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Requires Windows PowerShell"
)


def scenario(tmp_path, mode):
    # Every native operation is replaced before the entrypoint is called.
    # The real restoration decisions run; neither WSL nor owner state is touched.
    source = r"""
$ErrorActionPreference = 'Stop'
. '__LAUNCHER__' -LibraryOnly
$script:mode = '__MODE__'
$script:calls = @()
$script:restored = $false
function Invoke-EraWsl {
    param([string[]]$Command)
    $script:calls += ($Command -join ' ')
    $key = $Command -join ' '
    $code = 0; $text = ''; $errorText = ''
    if ($key -eq 'rig daemon status') {
        $text = if ($script:mode -in @('cold', 'start-failure') -and -not $script:restored) {
            'Daemon PID is absent (stale state)'
        } else { 'Daemon running on port 7433 (pid 42)' }
    } elseif ($key -eq 'rig daemon start --no-kernel') {
        if ($script:mode -eq 'start-failure') { $code = 1; $errorText = 'start refused' }
    } elseif ($key -eq 'rig ps --nodes --rig era-one --json') {
        if ($script:mode -eq 'bad-json') { $text = '{broken' }
        elseif ($script:mode -eq 'wrong-team') { $text = '[]' }
        else {
            $state = if ($script:mode -eq 'live' -or $script:restored) { 'running' } else { 'detached' }
            $text = @(
                @{canonicalSessionName='dev-build@era-one';sessionStatus=$state;intentionallyStopped=($script:mode -eq 'stopped')},
                @{canonicalSessionName='dev-review@era-one';sessionStatus=$state;intentionallyStopped=$false}
            ) | ConvertTo-Json -Compress
        }
    } elseif ($Command[0] -eq 'tmux') {
        $exists = $script:mode -eq 'live' -or $script:restored -or
            ($script:mode -eq 'partial' -and $key -match 'dev-build')
        if (-not $exists) {
            $code = 1; $errorText = "can't find session: " + $Command[-1]
            if ($script:mode -eq 'cold') { $errorText = 'error connecting to /tmp/tmux-1000/default (No such file or directory)' }
            if ($script:mode -eq 'no-server') { $errorText = 'no server running on /tmp/tmux-1000/default' }
            if ($script:mode -eq 'tmux-refused') { $errorText = 'error connecting to /tmp/tmux-1000/default (Connection refused)' }
            if ($script:mode -eq 'tmux-permission') { $errorText = 'error connecting to /tmp/tmux-1000/default (Permission denied)' }
            if ($script:mode -eq 'tmux-protocol') { $errorText = 'server exited unexpectedly' }
            if ($script:mode -eq 'tmux-empty') { $errorText = '' }
        }
        if ($script:mode -eq 'unknown-tmux') { $code = 2 }
    } elseif ($key -eq 'rig up era-one --existing --json') {
        if ($script:mode -eq 'restore-failure') { $code = 1; $text = '{"status":"failed"}' }
        elseif ($script:mode -eq 'restore-no-sessions') { $text = '{"status":"restored"}' }
        elseif ($script:mode -eq 'fresh-required') {
            $code = 1; $text = '{"status":"restored","nodes":[{"status":"awaiting-decision"}]}'
        } elseif ($script:mode -eq 'trust') {
            $code = 1; $script:restored = $true
            $text = '{"status":"restored","nodes":[{"status":"resumed"},{"status":"attention_required"}]}'
        } else { $script:restored = $true; $text = '{"status":"restored"}' }
    } else { throw "Unexpected command: $key" }
    [pscustomobject]@{ExitCode=$code;Text=$text;ErrorText=$errorText}
}
# Exercise the same restore flow twice: a second click must not restore twice.
$ok = $false; $message = ''
try { Initialize-EraTeam; Initialize-EraTeam; $ok = $true }
catch { $message = $_.Exception.Message }
@{ok=$ok;message=$message;calls=@($script:calls)} | ConvertTo-Json -Compress
"""
    source = source.replace("__LAUNCHER__", str(LAUNCHER).replace("'", "''"))
    source = source.replace("__MODE__", mode)
    script = tmp_path / "launcher-scenario.ps1"
    script.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_cold_start_restores_existing_conversations_once_before_attach(tmp_path):
    result = scenario(tmp_path, "cold")
    assert result["ok"], result["message"]
    calls = result["calls"]
    assert calls.count("rig up era-one --existing --json") == 1
    assert calls.index("rig daemon start --no-kernel") < calls.index(
        "rig up era-one --existing --json"
    )
    assert calls[-1].startswith("tmux has-session")
    assert not any("--fresh" in call or "rig.yaml" in call for call in calls)


def test_running_team_is_never_restored_or_restarted(tmp_path):
    result = scenario(tmp_path, "live")
    assert result["ok"]
    assert not any("rig up " in call or "daemon start" in call for call in result["calls"])


def test_daemon_failure_stops_before_team_recovery(tmp_path):
    result = scenario(tmp_path, "start-failure")
    assert not result["ok"]
    assert "start refused" in result["message"]
    assert not any("rig up " in call for call in result["calls"])


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "bad-json",
        "wrong-team",
        "unknown-tmux",
        "stopped",
        "tmux-refused",
        "tmux-permission",
        "tmux-protocol",
        "tmux-empty",
    ],
)
def test_uncertain_partial_or_deliberately_stopped_team_is_not_replaced(tmp_path, mode):
    result = scenario(tmp_path, mode)
    assert not result["ok"]
    assert not any("rig up " in call for call in result["calls"])


def test_confirmed_no_server_diagnostic_can_restore_saved_team(tmp_path):
    result = scenario(tmp_path, "no-server")
    assert result["ok"], result["message"]
    assert result["calls"].count("rig up era-one --existing --json") == 1


@pytest.mark.parametrize("mode", ["restore-failure", "restore-no-sessions", "fresh-required"])
def test_restore_failure_never_falls_back_to_a_new_conversation(tmp_path, mode):
    result = scenario(tmp_path, mode)
    assert not result["ok"]
    assert result["calls"].count("rig up era-one --existing --json") == 1


def test_restored_native_trust_prompt_can_be_shown_without_answering_it(tmp_path):
    result = scenario(tmp_path, "trust")
    assert result["ok"], result["message"]
    assert result["calls"].count("rig up era-one --existing --json") == 1
    assert not any("send-keys" in call or "rig send" in call for call in result["calls"])
