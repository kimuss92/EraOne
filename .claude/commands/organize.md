---
description: Organize EraOne from empty {got} file (daily + on ask)
arguments: []
---
# PowerShell — Windows-native (primary shell). Invoked via /organize or scheduled.
$root = Resolve-Path "..\.."  # .claude/commands/ -> repo root
$got = Join-Path $root "{got}"
$logDir = Join-Path $root "memory\organization"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$date = Get-Date -Format "yyyyMMdd-HHmmss"
& (Join-Path $root "scripts\organize_project.py") --apply --json
$out = Join-Path $logDir "$((Get-Date -Format 'yyyyMMdd'))-organize.json"
'{"source":"{got}","status":"organized","date":"' + (Get-Date -Format 'yyyy-MM-dd') + '","trigger":"user-ask / daily","shell":"powershell"}' | Set-Content $out
Write-Output "Organized from empty file {got} at $(Get-Date -Format 'o') -> $out"
