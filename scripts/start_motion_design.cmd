@echo off
rem One-click launcher for a Motion Design build. Double-click, or pass a brand and a title part:
rem   start_motion_design.cmd ceram_quartz
rem   start_motion_design.cmd delta_cuisine Alya
rem See start_motion_design.ps1 for what it does. It starts nothing automatically except Claude Code.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_motion_design.ps1" -Brand "%~1" -Brief "%~2"
