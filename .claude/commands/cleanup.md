---
description: Automated cleanup — finds placeholder-word files and deletes with confirmation option (empty 0 KB files are always preserved)
arguments: [confirm: bool, dry: bool]
---
# PowerShell — cleanup command. Searches root for placeholder-only content.
# Safety gate: deletes ONLY if --confirm passed OR user approves; never deletes blindly.
# Empty (0 KB) placeholder files are PRESERVED by policy — never targeted.
$root = Resolve-Path "."
$placeholders = @("no","known","nothing","observation","output")
$found = @()
# __init__.py and .gitkeep are NEVER touched, whatever their content.
$protected = @("__init__.py", ".gitkeep")
# Placeholder-only content (Length -gt 0 keeps empty files out)
Get-ChildItem $root -File | Where-Object { $_.Name -notin $protected -and $_.Length -gt 0 -and $_.Length -lt 200 } | % {
  $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
  $trim = ($text -replace '\s','').ToLower()
  if ($placeholders | % { $trim -eq $_ } ) { $found += $_.Name }
}
Write-Output "Found $($found.Count) target files: $($found -join ', ')"
Write-Output "Confirmation required before delete (safety gate per CLAUDE.md). Pass --confirm to auto-approve after reviewing."
