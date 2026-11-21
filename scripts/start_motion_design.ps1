# One-click launcher for a Motion Design build (Option A).
#
#   start_motion_design.cmd                      newest motion brief of any brand
#   start_motion_design.cmd ceram_quartz         newest Ceram Quartz brief
#   start_motion_design.cmd delta_cuisine "Alya"  newest Delta brief whose title contains "Alya"
#
# It opens Claude Code in the brand's project folder, allows the vault and the brand's texture and
# logo folders up front, and sends the first message for you:
#     start motion design from this brief: "<title>" (vault Inbox)
# Claude then ASKS you first: French text, or no text. Nothing is auto-approved: every permission
# prompt and every approval step of the skill still reaches you.
#
# ACCOUNT (Karim 2026-11-20): it uses YOUR CLAUDE ACCOUNT directly, not the OmniRoute gateway. A long
# interactive session cannot survive the gateway: its fallback chain broke on new request blocks
# (tool_addition needs an anthropic-beta header), signed thinking blocks and other models' formats.
# It runs in its own profile (C:\Users\nesri\.claude-motion), so your other Claude setups are never
# touched. The first time, sign in once: Claude opens, type /login and pick your Claude account.
#
#   -Gateway  use the OmniRoute gateway instead (not recommended for long sessions)
#   -DryRun   print what it would do and start nothing
param(
    [string]$Brand = "",
    [string]$Brief = "",
    [switch]$Gateway,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$profileDir = Join-Path $env:USERPROFILE ".claude-motion"

function Stop-WithMessage($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor Yellow
    Write-Host ""
    if (-not $DryRun) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Stop-WithMessage "Python was not found on PATH, so I cannot find your brief." }
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { Stop-WithMessage "Claude Code ('claude') was not found on PATH." }

Push-Location $repo
try {
    $raw = & $python -X utf8 -m crew.motion_brief launch-info $Brand $Brief 2>$null
} finally {
    Pop-Location
}
try { $info = ($raw | Select-Object -Last 1) | ConvertFrom-Json } catch { $info = $null }
if (-not $info) { Stop-WithMessage "I could not read the brief list. Run: python -m crew.motion_brief launch-info" }
if (-not $info.ok) { Stop-WithMessage $info.error }
if (-not $info.project_dir_exists) { Stop-WithMessage ("The project folder does not exist: " + $info.project_dir) }

Write-Host ""
Write-Host "Motion Design" -ForegroundColor Cyan
Write-Host ("  Brand  : " + $info.brand)
Write-Host ("  Brief  : " + $info.title)
Write-Host ("  Folder : " + $info.project_dir)

$signedIn = $true
if ($Gateway) {
    # --- OmniRoute gateway mode ---------------------------------------------------------------
    # A shortcut window inherits the environment Explorer loaded at login; re-read the gateway
    # settings from the registry (values are never printed). Interactive Claude Code only uses an
    # API key you approved once, so the same secret goes in as a bearer token (this window only).
    foreach ($name in "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL") {
        $fresh = [Environment]::GetEnvironmentVariable($name, "User")
        if ($fresh) { Set-Item -Path "env:$name" -Value $fresh }
    }
    if ($env:ANTHROPIC_API_KEY -and -not $env:ANTHROPIC_AUTH_TOKEN) { $env:ANTHROPIC_AUTH_TOKEN = $env:ANTHROPIC_API_KEY }
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
    Write-Host "  Account: OmniRoute gateway (-Gateway)" -ForegroundColor Yellow
} else {
    # --- your Claude account, directly --------------------------------------------------------
    foreach ($name in "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL") {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:CLAUDE_CONFIG_DIR = $profileDir
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path (Join-Path $profileDir "skills") | Out-Null
        # keep the skill in step with the repo copy (this profile starts empty, no plugins or hooks)
        $skillSrc = Join-Path $repo ".claude\skills\motion-design"
        if (Test-Path $skillSrc) { Copy-Item -Path $skillSrc -Destination (Join-Path $profileDir "skills") -Recurse -Force }
        $settings = Join-Path $profileDir "settings.json"
        if (-not (Test-Path $settings)) {
            # only what a build needs without asking every time; everything else still asks
            '{"permissions":{"allow":["Bash(node:*)","Bash(python:*)","Bash(ffmpeg:*)","Bash(ffprobe:*)"]}}' | Set-Content -Path $settings -Encoding UTF8
        }
    }
    $status = $null
    try { $status = (& $claude auth status 2>$null | Out-String) | ConvertFrom-Json } catch { $status = $null }
    $signedIn = [bool]($status -and $status.loggedIn)
    if ($signedIn) {
        Write-Host ("  Account: your Claude account (" + $status.authMethod + ")") -ForegroundColor Green
    } else {
        Write-Host "  Account: NOT SIGNED IN yet (one-time step)" -ForegroundColor Yellow
    }
}
Write-Host ""

if (-not $Gateway -and -not $signedIn) {
    Write-Host "FIRST TIME: sign in to your Claude account once." -ForegroundColor Yellow
    Write-Host "  1. Claude opens now. Type  /login  and choose your Claude account (your browser opens)."
    Write-Host "  2. When it says you are signed in, close this window."
    Write-Host "  3. Double-click Motion Design again: it will start the brief and ask French or no text."
    Write-Host ""
    if ($DryRun) { Write-Host "DRY RUN: would open Claude with no prompt so you can /login." -ForegroundColor Yellow; exit 0 }
    Set-Location $info.project_dir
    & $claude
    exit 0
}

Write-Host "Claude will ask you FIRST: French text, or no text?" -ForegroundColor Green
Write-Host ""

$claudeArgs = @()
foreach ($d in $info.add_dirs) { $claudeArgs += @("--add-dir", $d) }

if ($DryRun) {
    Write-Host "DRY RUN. Would run in $($info.project_dir):" -ForegroundColor Yellow
    Write-Host ('  claude "' + $info.sentence + '" ' + (($claudeArgs | ForEach-Object { '"' + $_ + '"' }) -join " "))
    exit 0
}

# The sentence contains double quotes. Windows PowerShell 5.1 (the one this launcher runs under)
# splits an argument like that into several and drops the quotes, so Claude would get garbage.
# Escaping them delivers it intact (tested 2026-11-20). PowerShell 7.3+ passes it correctly as is.
$sentence = $info.sentence
if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSNativeCommandArgumentPassing -eq 'Legacy') {
    $sentence = $sentence -replace '"', '\"'
}

# The prompt goes FIRST: --add-dir takes several values and swallowed a trailing prompt as a folder
# name ("was read as a value of --add-dir, not as a prompt", seen 2026-11-20).
Set-Location $info.project_dir
& $claude $sentence @claudeArgs
