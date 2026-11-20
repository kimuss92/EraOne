param(
    [switch]$PrepareOnly,
    [switch]$LibraryOnly
)

# Keep restoration separate from viewing: an attach command cannot start a team.
function Invoke-EraWsl {
    param([string[]]$Command)
    $arguments = @('-d', 'OpenRig-Ubuntu', '-u', 'nesri', '--') + $Command
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = 'wsl.exe'
    foreach ($value in $arguments) { $info.ArgumentList.Add($value) }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Could not start WSL.' }
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(180000)) {
            # An interrupted restore may already have launched a seat. Never retry
            # or kill the team merely because the caller cannot observe completion.
            throw 'WSL did not finish in 180 seconds. Recovery is unconfirmed; inspect OpenRig before retrying.'
        }
        [pscustomobject]@{
            ExitCode = $process.ExitCode
            Text = $output.GetAwaiter().GetResult()
            ErrorText = $errors.GetAwaiter().GetResult()
        }
    } finally {
        $process.Dispose()
    }
}

function Invoke-EraRig {
    param([string[]]$RigArguments)
    Invoke-EraWsl -Command (@('rig') + $RigArguments)
}

function Get-EraTeamNodes {
    $result = Invoke-EraRig -RigArguments @('ps', '--nodes', '--rig', 'era-one', '--json')
    if ($result.ExitCode -ne 0) { throw "Cannot inspect the saved team: $($result.Text) $($result.ErrorText)" }
    try { $nodes = @($result.Text | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'OpenRig returned unreadable team state. No restore was attempted.' }
    $expected = @('dev-build@era-one', 'dev-review@era-one')
    if ($nodes.Count -ne 2 -or
        @($nodes | Where-Object { $_.canonicalSessionName -notin $expected }).Count -ne 0 -or
        @($nodes.canonicalSessionName | Select-Object -Unique).Count -ne 2) {
        throw 'The saved era-one team does not contain the expected two seats. No new team was created.'
    }
    $nodes
}

function Get-EraLiveSessions {
    $live = @()
    foreach ($name in @('dev-build@era-one', 'dev-review@era-one')) {
        $result = Invoke-EraWsl -Command @('tmux', 'has-session', '-t', "=$name")
        if ($result.ExitCode -eq 0) { $live += $name }
        else {
            # tmux uses 1 for both absence and transport/permission failures.
            # Only an exact missing-session or ENOENT/no-server diagnostic is
            # affirmative absence. Empty/unrecognized errors remain unknown.
            $diagnostic = $result.ErrorText.Trim()
            $missingSession = "^can't find session: =?" + [regex]::Escape($name) + '$'
            $missingServer = '^error connecting to /tmp/tmux-[0-9]+/default \(No such file or directory\)$'
            $noServer = '^no server running on /tmp/tmux-[0-9]+/default$'
            $absent = $result.ExitCode -eq 1 -and (
                $diagnostic -match $missingSession -or
                $diagnostic -match $missingServer -or
                $diagnostic -match $noServer
            )
            if (-not $absent) { throw "Cannot inspect tmux session ${name}: $diagnostic. No restore was attempted." }
        }
    }
    $live
}

function Initialize-EraTeam {
    $status = Invoke-EraRig -RigArguments @('daemon', 'status')
    if ($status.ExitCode -ne 0 -or $status.Text -notmatch 'Daemon running on port') {
        Write-Host 'Starting the OpenRig service...' -ForegroundColor Cyan
        $started = Invoke-EraRig -RigArguments @('daemon', 'start', '--no-kernel')
        if ($started.ExitCode -ne 0) { throw "OpenRig could not start: $($started.Text) $($started.ErrorText)" }
    }

    $nodes = @(Get-EraTeamNodes)
    $live = @(Get-EraLiveSessions)
    if ($live.Count -eq 2) { return }
    if ($live.Count -ne 0 -or
        @($nodes | Where-Object { $_.sessionStatus -notin @('detached', 'stopped', 'dead', 'failed', 'exited') }).Count -ne 0) {
        throw 'The team is only partly available or its state is uncertain. Existing agents were left untouched; inspect OpenRig to recover the missing seat.'
    }
    if (@($nodes | Where-Object { $_.intentionallyStopped }).Count -gt 0) {
        throw 'An agent was deliberately stopped. OpenRig needs an explicit recovery decision; no agent was restarted.'
    }

    Write-Host 'Restoring the saved Era One conversations...' -ForegroundColor Cyan
    $restored = Invoke-EraRig -RigArguments @('up', 'era-one', '--existing', '--json')
    # A native trust/login prompt can make restore return nonzero while both
    # conversations exist. Display those panes so the person can answer it.
    if ($restored.ExitCode -ne 0) {
        try { $report = $restored.Text | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "Team recovery did not complete: $($restored.Text) $($restored.ErrorText)" }
        if ($report.status -ne 'restored' -or
            @($report.nodes | Where-Object { $_.status -notin @('resumed', 'attention_required') }).Count -gt 0) {
            throw "Team recovery needs attention: $($restored.Text). No fresh conversations were created."
        }
        Write-Warning 'A restored agent needs your attention. Its prompt will be visible in the team window.'
    }

    $live = @(Get-EraLiveSessions)
    if ($live.Count -ne 2) { throw 'Both saved terminal sessions were not restored. No empty terminal panes were opened.' }
    Write-Host 'Saved team restored.' -ForegroundColor Green
}

function Show-EraTeam {
    $views = @(
        '-w', 'new', 'new-tab', '--title', '"Era One - Claude builder"',
        'wsl.exe', '-d', 'OpenRig-Ubuntu', '-u', 'nesri', '--',
        'env', '-u', 'TMUX', '/usr/bin/tmux', 'attach', '-f', 'ignore-size', '-t', 'dev-build@era-one',
        ';', 'split-pane', '-H', '--title', '"Era One - Codex reviewer"',
        'wsl.exe', '-d', 'OpenRig-Ubuntu', '-u', 'nesri', '--',
        'env', '-u', 'TMUX', '/usr/bin/tmux', 'attach', '-f', 'ignore-size', '-t', 'dev-review@era-one'
    )
    Start-Process wt.exe -ArgumentList $views -ErrorAction Stop
}

if (-not $LibraryOnly) {
    $lock = New-Object System.Threading.Mutex($false, 'Local\EraOne-OpenRig-Launcher')
    $ownsLock = $false
    try {
        try { $ownsLock = $lock.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $ownsLock = $true }
        if (-not $ownsLock) {
            Write-Host 'The Era One team is already being opened.'
            exit 0
        }
        Initialize-EraTeam
        if (-not $PrepareOnly) { Show-EraTeam }
    } catch {
        Write-Host "Could not open the Era One team: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    } finally {
        if ($ownsLock) { $lock.ReleaseMutex() }
        $lock.Dispose()
    }
}
