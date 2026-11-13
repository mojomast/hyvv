. "$PSScriptRoot\common.ps1"
if (-not (Test-Path $Config)) { Copy-Item (Join-Path $Root 'config.example.json') $Config; Write-Host "Created $Config - edit projects, then re-run."; exit 1 }
if ((Test-Path $PidFile) -and (Get-Process -Id (Get-Content $PidFile) -EA 0)) { Write-Host 'Already running (use stop.ps1 first to restart).'; exit 0 }
# A stale launcher (e.g. a python child orphaned by an old stop.ps1) keeps the port and old config: clean it up.
$stale = @(Get-LauncherProcesses)
if ($stale.Count) { Write-Host "Cleaning up $($stale.Count) stale launcher process(es) before starting."; Stop-Launcher | Out-Null }
if (@(Get-LauncherProcesses).Count) { Write-Host 'FAIL: could not stop the stale launcher; run stop.ps1 and check Task Manager.'; exit 1 }
$port = Get-LauncherPort
$owner = Get-NetTCPConnection -LocalPort $port -State Listen -EA 0 | Select-Object -First 1
if ($owner) { Write-Host "FAIL: port $port is already bound by pid $($owner.OwningProcess) (not a launcher); refusing to start."; exit 1 }
$p = Start-Process uv -ArgumentList @('run','--python','3.12','--with','cua==0.5.0',"$Root\launcher.py",'serve','--config',$Config) -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -PassThru
Set-Content $PidFile $p.Id
Start-Sleep 3; & "$PSScriptRoot\status.ps1"
