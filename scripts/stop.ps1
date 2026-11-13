. "$PSScriptRoot\common.ps1"
$n = Stop-Launcher
$left = @(Get-LauncherProcesses).Count
if ($left) { Write-Host "FAIL: $left launcher process(es) still running"; exit 1 }
if ($n) { Write-Host "Stopped $n process(es) (attempt journal kept; running sandboxes keep running and are reattached on next start)." } else { Write-Host 'Not running.' }
