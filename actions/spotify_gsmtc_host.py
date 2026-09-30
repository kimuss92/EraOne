# actions/spotify_gsmtc_host.py
"""Persistent PowerShell host for Windows GSMTC (media transport) reads.

Why PowerShell: neither `winrt` nor `winsdk` ships a CPython 3.14 wheel, and the
repo has no comtypes-WinRT helper to clone (comtypes is used only for pycaw's
CoInitialize). PowerShell has WinRT for free via System.Runtime.WindowsRuntime,
and this repo already drives the Bluetooth radio through exactly this AsTask/Await
idiom (actions/computer_settings.py). Spawning powershell.exe per call is
150-400 ms, which blows the voice-loop budget, so one host is kept warm for the
Era process lifetime and fed JSON commands over stdin, JSON replies over stdout.

Lifecycle: lazy-spawned on first use, killed on interpreter exit via atexit. The
host is a child of the Era process, so era_supervisor.py's kill_tree (taskkill
/T) already reaps it when Era is force-stopped — no orphaned powershell.exe.

This module is the window-automation lane; it never touches the dormant bridge.
"""

from __future__ import annotations

import atexit
import base64
import json
import subprocess
import threading
from queue import Empty, Queue

_CREATE_NO_WINDOW = 0x08000000

# The loop script. Reuses the repo-proven Await/asTaskGeneric idiom from
# actions/computer_settings.py (_BT_RADIO_PS). Reads one JSON command per line
# from stdin, writes one compact JSON reply per line to stdout. The session
# manager is cached in script scope; the Spotify session is resolved per call
# (GetSessions() is a cheap sync call) so track changes are always seen.
_PS_LOOP = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    if (-not $task.Wait(5000)) { throw 'winrt call timed out' }
    $task.Result
}
[Void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows, ContentType=WindowsRuntime]
[Void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows, ContentType=WindowsRuntime]
[Void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus, Windows, ContentType=WindowsRuntime]
$PS = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus]

function Get-SpotifySession {
    if ($null -eq $script:mgr) {
        $script:mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    }
    foreach ($s in $script:mgr.GetSessions()) {
        if ($s.SourceAppUserModelId -like '*Spotify*') { return $s }
    }
    return $null
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $out = [ordered]@{ ok = $false; error = 'unknown op' }
    try {
        $cmd = $line | ConvertFrom-Json
        $s = Get-SpotifySession
        if ($null -eq $s) {
            $out = [ordered]@{ ok = $false; error = 'no active spotify session' }
        } else {
            switch ($cmd.op) {
                'current_track' {
                    $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
                    $tl = $s.GetTimelineProperties(); $pi = $s.GetPlaybackInfo()
                    $out = [ordered]@{ ok = $true; title = [string]$p.Title; artist = [string]$p.Artist;
                        album = [string]$p.AlbumTitle; track_number = [int]$p.TrackNumber;
                        position_ms = [int]$tl.Position.TotalMilliseconds; duration_ms = [int]$tl.EndTime.TotalMilliseconds;
                        is_playing = ($pi.PlaybackStatus -eq $PS::Playing); source_app = [string]$s.SourceAppUserModelId;
                        art_url = $null; art_stream_available = ($null -ne $p.Thumbnail) }
                }
                'playback_state' {
                    $st = $s.GetPlaybackInfo().PlaybackStatus
                    $out = [ordered]@{ ok = $true; is_playing = ($st -eq $PS::Playing);
                        is_paused = ($st -eq $PS::Paused); is_stopped = ($st -eq $PS::Stopped) }
                }
                'progress' {
                    $tl = $s.GetTimelineProperties()
                    $out = [ordered]@{ ok = $true; position_ms = [int]$tl.Position.TotalMilliseconds;
                        duration_ms = [int]$tl.EndTime.TotalMilliseconds }
                }
                'seek_exact' {
                    $ticks = [long]([double]$cmd.seconds * 10000000)
                    [Void](Await ($s.TryChangePlaybackPositionAsync($ticks)) ([bool]))
                    Start-Sleep -Milliseconds 100
                    $tl = $s.GetTimelineProperties()
                    $out = [ordered]@{ ok = $true; position_ms = [int]$tl.Position.TotalMilliseconds;
                        duration_ms = [int]$tl.EndTime.TotalMilliseconds }
                }
                'seek_relative' {
                    $tl = $s.GetTimelineProperties()
                    $end = $tl.EndTime.TotalSeconds
                    $target = $tl.Position.TotalSeconds + [double]$cmd.delta_seconds
                    if ($target -lt 0) { $target = 0 }
                    if ($end -gt 0 -and $target -gt $end) { $target = $end }
                    $ticks = [long]($target * 10000000)
                    [Void](Await ($s.TryChangePlaybackPositionAsync($ticks)) ([bool]))
                    Start-Sleep -Milliseconds 100
                    $tl = $s.GetTimelineProperties()
                    $out = [ordered]@{ ok = $true; position_ms = [int]$tl.Position.TotalMilliseconds;
                        duration_ms = [int]$tl.EndTime.TotalMilliseconds }
                }
            }
        }
    } catch {
        $script:mgr = $null  # force a fresh manager next call after any failure
        $out = [ordered]@{ ok = $false; error = ("gsmtc error: " + $_.Exception.Message) }
    }
    [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}
"""


class _Host:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._q: Queue = Queue()
        self._lock = threading.Lock()

    def _spawn(self) -> None:
        enc = base64.b64encode(_PS_LOOP.encode("utf-16-le")).decode("ascii")
        self._proc = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NoLogo",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                enc,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._q = Queue()
        threading.Thread(target=self._read_loop, args=(self._proc, self._q), daemon=True).start()
        atexit.register(self.shutdown)

    def _read_loop(self, proc: subprocess.Popen, q: Queue) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line:
                    q.put(line)
        except Exception:
            pass
        q.put(None)  # EOF sentinel: the host exited

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        # Graceful: closing stdin makes the PS ReadLine loop hit EOF and exit on
        # its own; fall back to a hard kill only if it lingers.
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=1.5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def request(self, op: dict, timeout: float = 6.0) -> dict:
        """Send one op, return its reply dict. Serialised: one command in flight
        at a time. Respawns the host if it died. Never raises."""
        with self._lock:
            if not self._alive():
                try:
                    self._spawn()
                except Exception as exc:
                    return {"ok": False, "error": f"gsmtc host spawn failed: {exc}"}
            while not self._q.empty():  # drop any stale line from a prior timeout
                try:
                    self._q.get_nowait()
                except Empty:
                    break
            try:
                self._proc.stdin.write(json.dumps(op) + "\n")  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except Exception:
                self._kill()
                return {"ok": False, "error": "gsmtc host write failed"}
            try:
                line = self._q.get(timeout=timeout)
            except Empty:
                self._kill()
                return {"ok": False, "error": "gsmtc host timed out"}
            if line is None:
                self._kill()
                return {"ok": False, "error": "gsmtc host exited"}
            try:
                return json.loads(line)
            except Exception:
                return {"ok": False, "error": "gsmtc host bad response"}

    def shutdown(self) -> None:
        with self._lock:
            self._kill()


_HOST = _Host()


def request(op: dict, timeout: float = 6.0) -> dict:
    return _HOST.request(op, timeout)


def shutdown() -> None:
    """Kill the host. Called via atexit; safe to call more than once."""
    _HOST.shutdown()
