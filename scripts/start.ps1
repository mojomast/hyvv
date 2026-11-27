. "$PSScriptRoot\common.ps1"
# How long (wall-clock seconds) to wait for the launcher to bind and answer loopback health.
# Startup is CPU-bound and grows with the attempt journal (~414 s measured at ~1300 attempts),
# so the default leaves ~3x margin. Override with HYVV_START_HEALTH_WAIT_S (launcher-admin.ps1
# passes its own configured wait down this way).
$HealthWaitSeconds = 1200
if ($env:HYVV_START_HEALTH_WAIT_S -match '^[0-9]+$') { $HealthWaitSeconds = [int]$env:HYVV_START_HEALTH_WAIT_S }
if (-not (Test-Path $Config)) { Copy-Item (Join-Path $Root 'config.example.json') $Config; Write-Host "Created $Config - edit projects, then re-run."; exit 1 }
if ((Test-Path $PidFile) -and (Get-Process -Id (Get-Content $PidFile) -EA 0)) { Write-Host 'Already running (use stop.ps1 first to restart).'; exit 0 }
# A stale launcher (e.g. a python child orphaned by an old stop.ps1) keeps the port and old config: clean it up.
$stale = @(Get-LauncherProcesses)
if ($stale.Count) { Write-Host "Cleaning up $($stale.Count) stale launcher process(es) before starting."; Stop-Launcher | Out-Null }
if (@(Get-LauncherProcesses).Count) { Write-Host 'FAIL: could not stop the stale launcher; run stop.ps1 and check Task Manager.'; exit 1 }
$port = Get-LauncherPort
$owner = Get-NetTCPConnection -LocalPort $port -State Listen -EA 0 | Select-Object -First 1
if ($owner) { Write-Host "FAIL: port $port is already bound by pid $($owner.OwningProcess) (not a launcher); refusing to start."; exit 1 }
$TaskName = 'hyvv-launcher'
$task = Get-ScheduledTask -TaskName $TaskName -EA 0
if ($task) {
  # Launch outside this session (an SSH session's job object kills its children on close).
  Remove-Item $PidFile -Force -EA 0
  Start-ScheduledTask -TaskName $TaskName
} elseif ($env:SSH_CONNECTION -or $env:SSH_ORIGINAL_COMMAND) {
  Write-Host "FAIL: scheduled task $TaskName missing; refusing to start inside the SSH session (it would die on disconnect)."; exit 1
} else {
  $p = Start-Process uv -ArgumentList @('run','--python','3.12','--with','cua==0.5.0',"$Root\launcher.py",'serve','--config',$Config) -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -PassThru
  Set-Content $PidFile $p.Id
}
# Loopback health, same rule as launcher-admin.ps1 status: HTTP 401 (auth required) or 200 is healthy.
# Do not use a token and do not use the configured bind address. Never Stop-Launcher here if the
# process is still coming up (a 45s probe used to kill a launcher that needed ~96s to bind).
function Test-LoopbackHealthy([int]$Port) {
  $url = "http://127.0.0.1:$Port/v1/health"
  try {
    $resp = Invoke-WebRequest -Uri $url -Method GET -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    return (([int]$resp.StatusCode -eq 200) -or ([int]$resp.StatusCode -eq 401))
  } catch {
    $code = $null
    $ex = $_.Exception
    if ($ex.PSObject.Properties['Response'] -and $ex.Response -and $ex.Response.PSObject.Properties['StatusCode']) {
      try { $code = [int]$ex.Response.StatusCode } catch { }
    }
    if (($null -eq $code) -and $ex.InnerException -and $ex.InnerException.PSObject.Properties['Response'] -and $ex.InnerException.Response) {
      try { $code = [int]$ex.InnerException.Response.StatusCode } catch { }
    }
    return (($code -eq 401) -or ($code -eq 200))
  }
}
$ok = $false
# A wall-clock deadline, not an iteration count: each pass also spends time on the process
# query and the probe, so 'N passes' used to mean several times N seconds.
$healthDeadline = (Get-Date).AddSeconds($HealthWaitSeconds)
while (-not $ok -and (Get-Date) -lt $healthDeadline) {
  Start-Sleep 1
  $procs = @(Get-LauncherProcesses)
  if (-not $procs.Count) { continue }
  if (Test-LoopbackHealthy $port) { $ok = $true }
}
if (-not $ok) {
  $still = @(Get-LauncherProcesses)
  if ($still.Count) {
    Write-Host "FAIL: launcher is still starting after ${HealthWaitSeconds}s (process running; loopback health not yet proven); leaving it running."
    exit 1
  }
  Write-Host "FAIL: launcher did not come up healthy from the install root; it is not running."
  exit 1
}
& "$PSScriptRoot\status.ps1"
# Explicit success code: launcher-admin.ps1 (StrictMode) reads $LASTEXITCODE after calling this script.
exit 0
