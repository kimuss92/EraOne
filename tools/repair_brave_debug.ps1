# Explicit maintenance operation: gracefully close Brave, then restore its session
# with loopback-only remote debugging. Never terminate a process forcibly.
$ErrorActionPreference = 'Stop'
$braveRoots = @(Get-CimInstance Win32_Process -Filter "Name='brave.exe'" | Where-Object {
    $_.CommandLine -notmatch '--type=' -and
    -not ($_.CommandLine -match '--headless' -and $_.CommandLine -match '--user-data-dir')
})
$braveProcesses = @($braveRoots | ForEach-Object {
    Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
})
$braveExecutable = $braveProcesses | Where-Object { $_.Path } | Select-Object -First 1 -ExpandProperty Path
if (-not $braveExecutable) {
    $braveCandidates = @(
        "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe",
        "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
    )
    $braveExecutable = $braveCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $braveExecutable) { throw 'Brave executable not found.' }
foreach ($braveProcess in $braveProcesses) {
    if ($braveProcess.MainWindowHandle -ne 0) {
        $null = $braveProcess.CloseMainWindow()
    }
}
$braveDeadline = (Get-Date).AddSeconds(20)
while (@($braveProcesses | Where-Object { -not $_.HasExited }).Count) {
    if ((Get-Date) -ge $braveDeadline) {
        throw 'Brave is still open. Resolve any save prompt and exit its tray process, then retry. Nothing was force-closed.'
    }
    Start-Sleep -Milliseconds 250
}
Start-Process -FilePath $braveExecutable -WindowStyle Hidden -ArgumentList @(
    '--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
    '--restore-last-session', '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding'
)
$braveDeadline = (Get-Date).AddSeconds(15)
do {
    try {
        $braveVersion = Invoke-RestMethod 'http://127.0.0.1:9222/json/version' -TimeoutSec 2
        if ($braveVersion.webSocketDebuggerUrl) {
            Write-Output 'Brave remote debugging is available on 127.0.0.1:9222.'
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $braveDeadline)
throw 'Brave restarted, but remote debugging is still unavailable.'
