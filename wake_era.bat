@echo off
rem ===========================================================================
rem  wake_era.bat - one double-click to bring Era back after an intentional
rem  shutdown. Runs `register_era_supervisor.ps1 -Action wake`, which clears the
rem  ~/.era/era.stop flag and restarts the supervisor (which relaunches Era).
rem
rem  No admin needed (start/stop of an existing task is unelevated).
rem  Portable: %~dp0 is THIS file's folder, so it finds the .ps1 beside it no
rem  matter where it's launched from (desktop shortcut, taskbar, Run box).
rem
rem  This is the seed of the fuller Era launcher - add more actions here later
rem  (e.g. a menu for wake / stop / status).
rem ===========================================================================
title Wake Era
echo Waking Era...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_era_supervisor.ps1" -Action wake
if errorlevel 1 (
    echo.
    echo [!] Something went wrong - see the message above.
    echo     You can also run it by hand:  register_era_supervisor.ps1 -Action wake
    echo.
    pause
) else (
    echo.
    echo This window will close in 4 seconds...
    timeout /t 4 >nul
)
