# Install or update hyvv on Windows. Replaces the version-specific idle-swap waiters.
#
#   .\scripts\install.ps1                 # copy this tree into %USERPROFILE%\hyvv
#   .\scripts\install.ps1 -Prefix D:\hyvv # custom install dir
#   .\scripts\install.ps1 -Update         # wait until the host is idle, then swap
#
# Never overwrites config.json, *.token, or *.key. Never prints secret values.
# A failed health check after an update rolls launcher.py back automatically.
param(
  [string]$Prefix = (Join-Path $env:USERPROFILE "hyvv"),
  [switch]$Update,
  [switch]$WhatIf,
  [int]$WaitSeconds = 3600,
  [string]$Source = ""
)

$ErrorActionPreference = "Stop"
if (-not $Source) { $Source = Split-Path -Parent $PSScriptRoot }

function L([string]$m) { Write-Host $m }
if ($WhatIf) {
  L "WhatIf: would install hyvv files to $Prefix (Update=$Update). Config, keys and tokens would not be overwritten."
  exit 0
}

$payload = @(
  "launcher.py",
  "agy_cua_mcp.py",
  "brief.schema.json",
  "result.schema.json",
  "config.example.json"
)

if (-not (Test-Path (Join-Path $Source "launcher.py"))) {
  throw "Source $Source does not contain launcher.py"
}

New-Item -ItemType Directory -Force $Prefix | Out-Null
$scriptsDst = Join-Path $Prefix "scripts"
New-Item -ItemType Directory -Force $scriptsDst | Out-Null

function Get-ConfigPath {
  $data = Join-Path $env:USERPROFILE ".orbit-launcher"
  return Join-Path $data "config.json"
}

function Get-Health {
  $cfgPath = Get-ConfigPath
  if (-not (Test-Path $cfgPath)) { return $null }
  try {
    $c = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $tokenFile = Join-Path $env:USERPROFILE ".orbit-launcher\orbit-api.token"
    if (-not (Test-Path $tokenFile)) { return $null }
    $t = (Get-Content $tokenFile -Raw).Trim()
    $bind = $c.bind; if (-not $bind) { $bind = "127.0.0.1" }
    $port = $c.port; if (-not $port) { $port = 47821 }
    return Invoke-RestMethod "http://$bind`:$port/v1/health" -Headers @{ Authorization = "Bearer $t" }
  } catch {
    return $null
  }
}

function Get-ActiveCount {
  $cfgPath = Get-ConfigPath
  if (-not (Test-Path $cfgPath)) { return 0 }
  try {
    $c = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $tokenFile = Join-Path $env:USERPROFILE ".orbit-launcher\orbit-api.token"
    if (-not (Test-Path $tokenFile)) { return 0 }
    $t = (Get-Content $tokenFile -Raw).Trim()
    $bind = $c.bind; if (-not $bind) { $bind = "127.0.0.1" }
    $port = $c.port; if (-not $port) { $port = 47821 }
    $hdr = @{ Authorization = "Bearer $t" }
    $h = Invoke-RestMethod "http://$bind`:$port/v1/health" -Headers $hdr
    $n = 0
    if ($h.active) { $n += [int]$h.active }
    if ($h.queued) { $n += [int]$h.queued }
    return $n
  } catch {
    return 0
  }
}

function Copy-Payload {
  foreach ($rel in $payload) {
    $from = Join-Path $Source $rel
    $to = Join-Path $Prefix $rel
    Copy-Item -Force $from $to
  }
  Get-ChildItem (Join-Path $Source "scripts") -Filter *.ps1 | ForEach-Object {
    if ($_.Name -eq "install.ps1" -or $_.Name -eq "install.sh") {
      Copy-Item -Force $_.FullName (Join-Path $scriptsDst $_.Name)
    } else {
      Copy-Item -Force $_.FullName (Join-Path $scriptsDst $_.Name)
    }
  }
  if (Test-Path (Join-Path $Source "scripts\install.sh")) {
    Copy-Item -Force (Join-Path $Source "scripts\install.sh") (Join-Path $scriptsDst "install.sh")
  }
}

# Fresh install: copy files, seed config from the example only when missing.
if (-not $Update) {
  Copy-Payload
  $data = Join-Path $env:USERPROFILE ".orbit-launcher"
  New-Item -ItemType Directory -Force $data | Out-Null
  $cfg = Join-Path $data "config.json"
  if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $Prefix "config.example.json") $cfg
    L "Created $cfg with bind 127.0.0.1. Edit projects and bind, then run scripts\\new-token.ps1 and scripts\\start.ps1. Existing keys and tokens were not touched."
  } else {
    L "Left existing config, keys and tokens unchanged."
  }
  L "Installed hyvv files to $Prefix"
  exit 0
}

# Update: wait until idle, swap launcher.py, health-check, roll back on failure.
L "Update: waiting up to $WaitSeconds s for zero running/queued attempts before swapping launcher.py"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
  $n = Get-ActiveCount
  if ($n -eq 0) { break }
  L "host is busy ($n active/queued); waiting 5s"
  Start-Sleep -Seconds 5
}
if ((Get-ActiveCount) -ne 0) { throw "timed out waiting for the host to go idle; no files were changed" }

$dstPy = Join-Path $Prefix "launcher.py"
$bak = Join-Path $Prefix ("launcher.py.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
if (Test-Path $dstPy) { Copy-Item $dstPy $bak; L "backed up current launcher.py" }

$healthBefore = Get-Health
Copy-Payload
L "copied updated files to $Prefix (config, keys and tokens were not written)"

# Restart is left to start.ps1 / stop.ps1 so this script never kills a host that is mid-swap
# unless the operator already stopped it. If it is still answering, confirm health.
Start-Sleep -Seconds 1
$health = Get-Health
if (-not $health) {
  if ($healthBefore) {
    if (Test-Path $bak) {
      Copy-Item -Force $bak $dstPy
      L "health check failed; rolled launcher.py back to the backup"
    }
    throw "update aborted: host did not answer health after the swap (launcher.py rolled back if a backup existed)"
  }
  L "host is not running; files updated. Start it with scripts\\start.ps1 when ready."
  exit 0
}
L ("health ok version=" + $health.version)
exit 0
