@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  setup_claude_dirs.bat
REM  1) Junctions a SHARED skills folder into .claude-delta and .claude-freelance
REM     (maintain skills in ONE place; both accounts see them, no duplication).
REM  2) Reports the MCP servers configured in each account's config dir, so you
REM     can see whether Composio (Gmail / Facebook) is connected per account.
REM
REM  Safe to re-run. Junctions (/J) need no admin rights. Nothing is deleted.
REM ============================================================================

REM ---- Edit these if your layout differs -------------------------------------
set "SRC=%USERPROFILE%\.claude"
set "DIR_DELTA=%USERPROFILE%\.claude-delta"
set "DIR_FREELANCE=%USERPROFILE%\.claude-freelance"
REM ----------------------------------------------------------------------------

echo(
echo === Claude Code config setup =============================================
echo Shared skills source : %SRC%\skills
echo Delta account dir    : %DIR_DELTA%
echo Freelance account dir: %DIR_FREELANCE%
echo(

REM ---- 0) Is the claude CLI on PATH? -----------------------------------------
where claude >nul 2>&1
if errorlevel 1 (
  echo [!] "claude" is not on your PATH. The MCP check will be skipped.
  echo     Open the terminal where Claude Code works, or add it to PATH, then re-run.
  set "NOCLI=1"
) else (
  set "NOCLI="
)

REM ---- 1) Ensure the two account dirs exist ----------------------------------
if not exist "%DIR_DELTA%\"     mkdir "%DIR_DELTA%"
if not exist "%DIR_FREELANCE%\" mkdir "%DIR_FREELANCE%"

REM ---- 2) Junction the SHARED skills folder into each account dir -------------
if not exist "%SRC%\skills\" (
  echo [i] No shared skills folder yet at "%SRC%\skills".
  echo     Nothing to link. Create it and put SKILL.md folders inside, then re-run:
  echo         mkdir "%SRC%\skills"
  echo     ^(Or keep CM skills at project level in each brand's .claude\skills\ instead.^)
) else (
  call :linkskills "%DIR_DELTA%"
  call :linkskills "%DIR_FREELANCE%"
)

echo(
echo --- Plugins note ------------------------------------------------------------
echo Plugin FILES can be shared the same way, but a plugin's ENABLED state and its
echo MCP config live per config dir, so sharing the folder is not enough on its own.
echo If you still want to link the plugin files, run manually:
echo     mklink /J "%DIR_DELTA%\plugins"     "%SRC%\plugins"
echo     mklink /J "%DIR_FREELANCE%\plugins" "%SRC%\plugins"

REM ---- 3) Report MCP servers per account config dir --------------------------
echo(
echo === MCP config per account ===============================================
if defined NOCLI (
  echo [skipped] claude CLI not found on PATH.
) else (
  call :mcpcheck "DELTA"     "%DIR_DELTA%"
  call :mcpcheck "FREELANCE" "%DIR_FREELANCE%"
  echo(
  echo Expected: both accounts should list Composio ^(for Gmail notifications;
  echo   Delta Cuisine also uses Composio for Facebook^). If an account is missing it,
  echo   add it in that dir, e.g.:
  echo     set "CLAUDE_CONFIG_DIR=%DIR_FREELANCE%"
  echo     claude   ^> then use /mcp  or:  claude mcp add ...
)

echo(
echo === Done. ================================================================
echo If a "skills" line says "created", the junction is set. Re-run any time.
endlocal
pause
goto :eof

REM ---------------------------------------------------------------------------
:linkskills
set "TARGET=%~1\skills"
if exist "%TARGET%" (
  echo [ok]      skills already present: %TARGET%
) else (
  mklink /J "%TARGET%" "%SRC%\skills" >nul 2>&1
  if errorlevel 1 (
    echo [FAILED]  could not create junction: %TARGET%
  ) else (
    echo [created] skills junction: %TARGET%  -^>  %SRC%\skills
  )
)
goto :eof

REM ---------------------------------------------------------------------------
:mcpcheck
echo(
echo ---- %~1 : %~2
set "CLAUDE_CONFIG_DIR=%~2"
claude mcp list 2>nul
if errorlevel 1 (
  echo [!] Could not read MCP list here ^(dir may not be logged in yet^).
  echo     Log in once:  set "CLAUDE_CONFIG_DIR=%~2"  ^&  claude   ^> /login
)
set "CLAUDE_CONFIG_DIR="
goto :eof
