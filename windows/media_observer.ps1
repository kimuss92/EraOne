# Read-only GSMTC observer. Callbacks only signal; no PowerShell callback runspace.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -ReferencedAssemblies System.Core -TypeDefinition @"
using System;
using System.Linq.Expressions;
using System.Reflection;
using System.Threading;
public sealed class EraMediaSubscription : IDisposable {
    private object source;
    private MethodInfo remove;
    private object token;
    private Delegate handler;
    public EraMediaSubscription(object target, string name, ManualResetEventSlim signal) {
        source = target;
        EventInfo ev = target.GetType().GetEvent(name);
        if (ev == null) throw new InvalidOperationException("missing media event");
        var args = Array.ConvertAll(ev.EventHandlerType.GetMethod("Invoke").GetParameters(),
            p => Expression.Parameter(p.ParameterType, p.Name));
        handler = Expression.Lambda(ev.EventHandlerType,
            Expression.Call(Expression.Constant(signal), typeof(ManualResetEventSlim).GetMethod("Set")), args).Compile();
        remove = ev.GetRemoveMethod();
        token = ev.GetAddMethod().Invoke(source, new object[] { handler });
    }
    public void Dispose() {
        if (source != null) { remove.Invoke(source, new object[] { token }); source = null; }
    }
}
"@
[Void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$task = $asTask.MakeGenericMethod([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]).Invoke(
    $null, @([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()))
if (-not $task.Wait(5000)) { throw 'media manager timeout' }
$manager = $task.Result
$signal = New-Object System.Threading.ManualResetEventSlim($false)
$managerSubscription = New-Object EraMediaSubscription($manager, 'SessionsChanged', $signal)
$entries = New-Object 'System.Collections.Generic.Dictionary[object,object]'
$nextId = 0
$cause = 'initial'
try {
    while ($true) {
        # Reset before sampling: an event during the read remains signalled.
        $signal.Reset()
        $current = @($manager.GetSessions())
        foreach ($old in @($entries.Keys)) {
            if ($current -notcontains $old) {
                foreach ($sub in $entries[$old].subscriptions) { $sub.Dispose() }
                [Void]$entries.Remove($old)
            }
        }
        $rows = @()
        foreach ($session in $current) {
            if (-not $entries.ContainsKey($session)) {
                $nextId++
                $subscriptions = @()
                try {
                    $subscriptions += New-Object EraMediaSubscription($session, 'PlaybackInfoChanged', $signal)
                    $subscriptions += New-Object EraMediaSubscription($session, 'MediaPropertiesChanged', $signal)
                } catch {
                    foreach ($sub in $subscriptions) { $sub.Dispose() }
                    throw
                }
                $entries.Add($session, @{ id = [string]$nextId; subscriptions = $subscriptions })
            }
            $playback = $session.GetPlaybackInfo()
            $rows += @{ id = $entries[$session].id; app = [string]$session.SourceAppUserModelId;
                status = [string]$playback.PlaybackStatus }
        }
        $message = @{ version = 1; cause = $cause; sessions = @($rows) } | ConvertTo-Json -Compress -Depth 5
        [Console]::Out.WriteLine($message)
        [Console]::Out.Flush()
        $eventArrived = $signal.Wait(15000)
        $cause = if ($eventArrived) { 'event' } else { 'reconcile' }
    }
} finally {
    foreach ($entry in $entries.Values) {
        foreach ($sub in $entry.subscriptions) { $sub.Dispose() }
    }
    $managerSubscription.Dispose()
}
