$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $env:USERPROFILE '.orbit-launcher'
$Config = Join-Path $Data 'config.json'
$PidFile = Join-Path $Data 'launcher.pid'
$Log = Join-Path $Data 'launcher.log'
New-Item -ItemType Directory -Force $Data | Out-Null
function Lock-UserOnly($p) { icacls $p /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null }
# uv/cua may not be on PATH in non-interactive shells: resolve them explicitly.
if (-not (Get-Command uv -EA 0)) { $u = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe" -EA 0 | Select -First 1; if ($u) { $env:PATH = "$(Split-Path $u.FullName);$env:PATH" } }
function Invoke-Cua { uv run -q --python 3.12 --with cua==0.5.0 cua @args }
$PyPidFile = Join-Path $Data 'launcher.py.pid'
# Every launcher process (uv wrapper and its python child), matched by command line.
function Get-LauncherProcesses { Get-CimInstance Win32_Process -EA 0 | Where-Object { $_.CommandLine -and $_.CommandLine -like '*launcher.py*' -and $_.CommandLine -like '* serve*' } }
function Stop-Launcher {
  $ids = @()
  foreach ($f in @($PidFile, $PyPidFile)) { if (Test-Path $f) { $ids += [int](Get-Content $f -Raw).Trim(); Remove-Item $f -Force } }
  $ids += @(Get-LauncherProcesses | ForEach-Object { [int]$_.ProcessId })
  $ids = $ids | Where-Object { $_ } | Sort-Object -Unique
  foreach ($i in $ids) { Stop-Process -Id $i -Force -EA 0 }
  Start-Sleep -Milliseconds 500
  return @($ids).Count
}
function Get-LauncherPort { try { (Get-Content $Config -Raw | ConvertFrom-Json).port } catch { 47821 } }
