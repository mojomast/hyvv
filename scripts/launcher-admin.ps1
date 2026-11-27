# Limited launcher admin for moltussy Hermes.
# Forced-command only: parse SSH_ORIGINAL_COMMAND (or argv when run locally). No shell eval.
# Writes may change config.json "projects" entries only.
# 'logs' is read-only. 'apply-update' swaps a fixed list of launcher payload files from a
# dedicated hyvv checkout (never scripts/, config, keys or tokens) and restarts when idle.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$ScriptDir\common.ps1"
# A worker PC's common.ps1 pins $Python to its uv interpreter; the repo copy may not.
if (-not (Get-Variable -Name Python -Scope Script -ErrorAction SilentlyContinue)) { $Python = '3.12' }

$FixturesRoot = Join-Path $env:USERPROFILE 'orbit-worker-fixtures'
$LockPath = Join-Path $Data 'admin.lock'
$AuditPath = Join-Path $Data 'admin-audit.jsonl'
$AllowedOps = @('status','list-projects','add-project','remove-project','restart-idle','logs','apply-update')
$ForbiddenConfigKeys = @('bind','extra_binds','go_key_file','deepseek_key_file','api_token_file','token','tokens','coordinators')
$BeeActiveStates = @('starting','running','cancel_requested')
$RestartWaitSeconds = 1800
$HealthRetries = 20
$HealthDelaySeconds = 3

# --- logs ---
$AttemptsDir = Join-Path $Data 'attempts'
$AttemptIdPattern = '^att_[0-9a-f]{8,32}$'
$LogsDefaultLines = 200
$LogsMaxLines = 1000

# --- apply-update: the two constants filled in at apply time ---
# No credential is read by this script: idle = strict attempts-journal check, liveness = unauthenticated
# loopback 401 + its Server header, deployed version = hyvv-deployed.json written at install time.
# BLOCKER (2026-11-12): this checkout does not exist yet, and hyvv origin/main does not contain
# the launcher (PR #1 unmerged). Create it with a plain clone of mojomast/hyvv owned by kyle.
# It must NOT be a bee fixture under orbit-worker-fixtures (bees/sync touch those).
$HyvvCheckout = Join-Path $env:USERPROFILE 'hyvv-deploy'
# "Install" = copy exactly these files from the checkout into $Root (the dir start.ps1 runs
# launcher.py from), then warm the same uv env start.ps1 uses. scripts\*.ps1 (including this file),
# config.json, *.token and *.key are never copied, so a merge to main cannot rewrite these guardrails.
$InstallPayload = @('launcher.py','agy_cua_mcp.py','brief.schema.json','result.schema.json','provisioning.py','landing_export.py')
$InstallUvArgs = @('run','-q','--python',$Python,'--with','cua==0.5.0','python','-c','import cua')
$HyvvRepoName = 'hyvv'
$HyvvOriginUrl = 'https://github.com/mojomast/hyvv.git'
$HyvvMainRef = 'refs/remotes/origin/main'
$BeeBusyStates = @('starting','running','cancel_requested','queued','backoff')
$KnownAttemptStates = @('queued','backoff','starting','running','cancel_requested','succeeded','failed','cancelled')
$DeployedVersionFile = 'hyvv-deployed.json'
$IdleRecheckSeconds = 3
$GitTimeoutSeconds = 300

# Non-interactive SSH sessions may have a short PATH.
if (-not (Get-Command python -EA 0)) {
  $candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) { $env:PATH = "$(Split-Path $c);$env:PATH"; break }
  }
}
function Get-PythonExe {
  $cmd = Get-Command python -EA 0
  if ($cmd) { return $cmd.Source }
  $cmd = Get-Command py -EA 0
  if ($cmd) { return $cmd.Source }
  throw 'python not found on PATH; cannot edit config.json safely'
}

function Write-Scrubbed([string]$text) {
  if ([string]::IsNullOrEmpty($text)) { return }
  $s = [regex]::Replace($text, '\b\d{1,3}(?:\.\d{1,3}){3}\b', '<ip>')
  $s = [regex]::Replace($s, 'https?://\S+', '<url>')
  $s = [regex]::Replace($s, '(?i)(Authorization:\s*Bearer\s+)\S+', '${1}<redacted>')
  Write-Output $s
}

function Format-RedactedLogLine([string]$line) {
  if ($null -eq $line) { return '' }
  $s = $line
  # Authorization headers (any scheme), then bare bearer tokens.
  $s = [regex]::Replace($s, '(?i)(\bAuthorization\b["'']?\s*[:=]\s*["'']?)(?:Bearer\s+|Basic\s+|Token\s+)?[^\s"'',;]+', '${1}<redacted>')
  $s = [regex]::Replace($s, '(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+', '${1}<redacted>')
  # key/token/secret/password style assignments: k=v, k: v, "k": "v", including api_key, x-api-key, *_TOKEN etc.
  $s = [regex]::Replace($s, '(?i)(["'']?[A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key|\bkey)[A-Za-z0-9_.-]*["'']?\s*[:=]\s*)("[^"]*"|''[^'']*''|[^\s,;}\]]+)', '${1}<redacted>')
  # Well-known credential shapes.
  $s = [regex]::Replace($s, '\bsk-[A-Za-z0-9_-]{8,}', '<redacted>')
  $s = [regex]::Replace($s, '\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}', '<redacted>')
  $s = [regex]::Replace($s, '\bgithub_pat_[A-Za-z0-9_]{20,}', '<redacted>')
  $s = [regex]::Replace($s, '\b(?:xox[abposr]|AKIA|ASIA)[A-Za-z0-9-]{10,}', '<redacted>')
  $s = [regex]::Replace($s, '\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}', '<redacted-jwt>')
  $s = [regex]::Replace($s, '-----BEGIN [A-Z ]*PRIVATE KEY-----.*', '<redacted-private-key>')
  # Long hex runs (>=48, so 40-char git shas survive) and long mixed-case base64url runs.
  $s = [regex]::Replace($s, '\b[0-9a-fA-F]{48,}\b', '<redacted-hex>')
  $s = [regex]::Replace($s, '(?<![A-Za-z0-9_+/=-])(?=[A-Za-z0-9_+/=-]{40,})(?=[^ ]*[a-z])(?=[^ ]*[A-Z])(?=[^ ]*[0-9])[A-Za-z0-9_+/=-]{40,}', '<redacted-blob>')
  # Same host scrubbing as Write-Scrubbed (no IPs, tailnet names or private URLs).
  $s = [regex]::Replace($s, '\b\d{1,3}(?:\.\d{1,3}){3}\b', '<ip>')
  $s = [regex]::Replace($s, '(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b', '<host>')
  $s = [regex]::Replace($s, '(?i)\bfd7a:115c:a1e0:[0-9a-f:]*', '<ip6>')
  $s = [regex]::Replace($s, 'https?://\S+', '<url>')
  return $s
}

function ConvertTo-JsonLine($obj) {
  ($obj | ConvertTo-Json -Compress -Depth 8)
}

function Write-Audit($op, $ok, $detail, $backup, $argsObj) {
  New-Item -ItemType Directory -Force $Data | Out-Null
  $rec = [ordered]@{
    ts = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    op = $op
    ok = [bool]$ok
    detail = [string]$detail
    backup = $backup
    args = $argsObj
    user = $env:USERNAME
    src = $(if ($env:SSH_ORIGINAL_COMMAND) { 'ssh' } else { 'local' })
  }
  $line = ConvertTo-JsonLine $rec
  $line = [regex]::Replace($line, '\b\d{1,3}(?:\.\d{1,3}){3}\b', '<ip>')
  Add-Content -LiteralPath $AuditPath -Value $line -Encoding utf8
}

function Split-AdminArgs([string]$line) {
  if ($null -eq $line) { $line = '' }
  $line = $line.Trim()
  $result = New-Object System.Collections.Generic.List[string]
  $sb = New-Object System.Text.StringBuilder
  $inQuotes = $false
  for ($i = 0; $i -lt $line.Length; $i++) {
    $c = $line[$i]
    if ($c -eq [char]34) { $inQuotes = -not $inQuotes; continue }
    if (-not $inQuotes -and [char]::IsWhiteSpace($c)) {
      if ($sb.Length -gt 0) { [void]$result.Add($sb.ToString()); [void]$sb.Clear() }
      continue
    }
    if ([int]$c -lt 32) { throw 'invalid control character in command' }
    [void]$sb.Append($c)
  }
  if ($inQuotes) { throw 'unclosed quote in command' }
  if ($sb.Length -gt 0) { [void]$result.Add($sb.ToString()) }
  return ,$result.ToArray()
}

function Get-AdminArgv {
  $raw = $env:SSH_ORIGINAL_COMMAND
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return ,@($argsFromMain)
  }
  return ,(Split-AdminArgs $raw)
}

function Test-RepoSpec([string]$spec) {
  if ($spec -notmatch '^(?i)mojomast/([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)$') {
    throw "repo must be mojomast/<name> with a safe name, got '$spec'"
  }
  $name = $Matches[1]
  return @{ Owner = 'mojomast'; Name = $name; Spec = "mojomast/$name"; Url = "https://github.com/mojomast/$name.git" }
}

function Test-SyncBranch([string]$branch) {
  if ([string]::IsNullOrWhiteSpace($branch)) { return $null }
  if ($branch -match '\.\.|//' -or $branch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$') {
    throw "invalid sync_branch"
  }
  return $branch
}

function Resolve-FixturePath([string]$path) {
  if ([string]::IsNullOrWhiteSpace($path)) { throw 'fixture path is required' }
  if ($path -match '[;&|`$<>]') { throw 'fixture path contains forbidden characters' }
  $full = [System.IO.Path]::GetFullPath($path)
  $root = [System.IO.Path]::GetFullPath($FixturesRoot).TrimEnd('\') + '\'
  $cmp = $full.TrimEnd('\') + '\'
  if (-not $cmp.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "fixture path must be under $FixturesRoot"
  }
  if ($full.TrimEnd('\') -eq $root.TrimEnd('\')) {
    throw 'fixture path must be a checkout inside the fixtures directory, not the directory itself'
  }
  if (-not (Test-Path -LiteralPath $full -PathType Container)) {
    throw "fixture path does not exist: $full"
  }
  if (-not (Test-Path -LiteralPath (Join-Path $full '.git'))) {
    throw "fixture path is not a git checkout: $full"
  }
  return $full
}

function Get-ActiveBeeCount {
  $dir = Join-Path $Data 'attempts'
  $n = 0
  if (-not (Test-Path -LiteralPath $dir)) { return 0 }
  Get-ChildItem -LiteralPath $dir -Filter '*.json' -File -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Name -like '*.result.json') { return }
    try {
      $a = Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8 | ConvertFrom-Json
      if ($BeeActiveStates -contains [string]$a.state) { $script:nInc = 1; $n++ }
    } catch { }
  }
  return $n
}

function Get-BeeStateCounts {
  $dir = Join-Path $Data 'attempts'
  $map = @{}
  $active = 0
  if (Test-Path -LiteralPath $dir) {
    Get-ChildItem -LiteralPath $dir -Filter '*.json' -File -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.Name -like '*.result.json') { return }
      try {
        $a = Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8 | ConvertFrom-Json
        $st = [string]($a.state)
        if (-not $st) { $st = 'unknown' }
        if ($map.ContainsKey($st)) { $map[$st]++ } else { $map[$st] = 1 }
        if ($BeeActiveStates -contains $st) { $active++ }
      } catch { }
    }
  }
  return @{ Active = $active; Counts = $map }
}

function Get-LauncherRunning {
  return (@(Get-LauncherProcesses).Count -gt 0)
}

function Get-LoopbackHealth {
  $port = Get-LauncherPort
  $url = "http://127.0.0.1:$port/v1/health"
  try {
    $resp = Invoke-WebRequest -Uri $url -Method GET -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    return [int]$resp.StatusCode
  } catch {
    $ex = $_.Exception
    $resp = $null
    if ($ex.Response) { $resp = $ex.Response }
    elseif ($ex.InnerException -and $ex.InnerException.Response) { $resp = $ex.InnerException.Response }
    if ($resp -and $resp.StatusCode) { return [int]$resp.StatusCode }
    return $null
  }
}

function Test-LauncherHealthy {
  $code = Get-LoopbackHealth
  # Auth is required on every /v1 route; 401 means the process is up.
  return ($code -eq 401 -or $code -eq 200), $code
}

function Wait-LauncherHealthy {
  for ($i = 0; $i -lt $HealthRetries; $i++) {
    $ok, $code = Test-LauncherHealthy
    if ($ok) { return $code }
    Start-Sleep -Seconds $HealthDelaySeconds
  }
  $ok, $code = Test-LauncherHealthy
  if (-not $ok) { throw "launcher health check failed (http=$(if ($null -eq $code) { 'unreachable' } else { $code }))" }
  return $code
}

function Backup-Config([string]$op) {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $dest = Join-Path $Data ("config.json.bak-{0}-{1}" -f $op, $stamp)
  Copy-Item -LiteralPath $Config -Destination $dest -Force
  return $dest
}

function Edit-ProjectsOnly([scriptblock]$mutator) {
  $py = @'
import json, sys, os
path, op = sys.argv[1], sys.argv[2]
payload = sys.stdin.read()
data = json.loads(open(path, encoding="utf-8").read())
before = json.loads(json.dumps(data))
projects = data.get("projects")
if not isinstance(projects, dict):
    raise SystemExit("projects is missing or not an object")
forbidden = {"bind","extra_binds","go_key_file","deepseek_key_file","api_token_file","token","tokens","coordinators"}
if op == "add":
    spec = json.loads(payload)
    name = spec["name"]
    if name in projects:
        raise SystemExit("project already exists: " + name)
    entry = {"path": spec["path"], "repo": spec["repo"]}
    if spec.get("sync_branch"):
        entry["sync_branch"] = spec["sync_branch"]
    projects[name] = entry
    data["projects"] = projects
elif op == "remove":
    spec = json.loads(payload)
    name = spec["name"]
    if name not in projects:
        # also allow lookup by repo url
        raise SystemExit("project not found: " + name)
    del projects[name]
    data["projects"] = projects
else:
    raise SystemExit("unknown edit op")
# only projects may differ
keys_before = set(before)
keys_after = set(data)
if keys_before != keys_after:
    raise SystemExit("refusing edit that adds/removes top-level keys")
for k in before:
    if k == "projects":
        continue
    if before[k] != data[k]:
        raise SystemExit("refusing edit that changes non-projects field: " + k)
    if k.lower() in forbidden:
        if before[k] != data[k]:
            raise SystemExit("forbidden field changed: " + k)
# write atomically
tmp = path + ".tmp-admin"
with open(tmp, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("ok")
'@
  $tmpPy = Join-Path $Data 'admin-edit-projects.py'
  Set-Content -LiteralPath $tmpPy -Value $py -Encoding utf8
  try {
    & $mutator $tmpPy
    if ($LASTEXITCODE -ne 0) { throw "config edit failed" }
  } finally {
    if (Test-Path -LiteralPath $tmpPy) { Remove-Item -LiteralPath $tmpPy -Force -ErrorAction SilentlyContinue }
  }
}

function Acquire-AdminLock {
  New-Item -ItemType Directory -Force $Data | Out-Null
  $deadline = (Get-Date).AddMinutes(2)
  do {
    try {
      $fs = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
      return $fs
    } catch {
      if ((Get-Date) -ge $deadline) { throw 'could not acquire admin lock within 2 minutes' }
      Start-Sleep -Milliseconds 250
    }
  } while ($true)
}

function Invoke-Status {
  $running = Get-LauncherRunning
  $bees = Get-BeeStateCounts
  $healthy, $code = if ($running) { Test-LauncherHealthy } else { $false, $null }
  $proj = 0
  if (Test-Path -LiteralPath $Config) {
    $cfg = Get-Content -LiteralPath $Config -Raw -Encoding utf8 | ConvertFrom-Json
    if ($cfg.projects) { $proj = @($cfg.projects.PSObject.Properties).Count }
  }
  $states = ($bees.Counts.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ','
  Write-Output "launcher_running=$(if ($running) { 'yes' } else { 'no' })"
  Write-Output "health_http=$(if ($null -eq $code) { 'unreachable' } else { $code })"
  Write-Output "health_ok=$(if ($healthy) { 'yes' } else { 'no' })"
  $rt = Join-Path $Data 'runtime_status.json'
  $live = 'unknown'; $jact = $bees.Active; $tmpn = 0; $split = 'unknown'
  if (Test-Path -LiteralPath $rt) {
    try {
      $rs = Get-Content -LiteralPath $rt -Raw -Encoding utf8 | ConvertFrom-Json
      if ($null -ne $rs.active) { $live = [string]$rs.active }
      if ($null -ne $rs.journal_active) { $jact = $rs.journal_active }
      if ($null -ne $rs.journal_tmp) { $tmpn = $rs.journal_tmp }
      if ($null -ne $rs.split_brain) { $split = $(if ($rs.split_brain) { 'yes' } else { 'no' }) }
    } catch { }
  }
  $tmpFiles = @()
  if (Test-Path -LiteralPath (Join-Path $Data 'attempts')) {
    $tmpFiles = @(Get-ChildItem -LiteralPath (Join-Path $Data 'attempts') -Filter '*.tmp' -File -Force -ErrorAction SilentlyContinue)
    if ($tmpFiles.Count -gt $tmpn) { $tmpn = $tmpFiles.Count }
  }
  if ($split -eq 'unknown' -and $live -ne 'unknown') {
    $split = $(if ([int]$live -ne [int]$jact -or $tmpn -gt 0) { 'yes' } else { 'no' })
  }
  Write-Output "bee_active=$live"
  Write-Output "bee_journal_active=$jact"
  Write-Output "bee_journal_tmp=$tmpn"
  Write-Output "bee_split_brain=$split"
  Write-Output "bee_states=$states"
  Write-Output "project_count=$proj"
  Write-Output "port=$(Get-LauncherPort)"
}

function Invoke-ListProjects {
  $cfg = Get-Content -LiteralPath $Config -Raw -Encoding utf8 | ConvertFrom-Json
  $props = @($cfg.projects.PSObject.Properties)
  Write-Output "project_count=$($props.Count)"
  foreach ($p in ($props | Sort-Object Name)) {
    $v = $p.Value
    $repo = ''
    $path = ''
    $branch = ''
    if ($v.PSObject.Properties['repo']) { $repo = [string]$v.repo }
    if ($v.PSObject.Properties['path']) { $path = [string]$v.path }
    if ($v.PSObject.Properties['sync_branch']) { $branch = [string]$v.sync_branch }
    Write-Output ("name={0} repo={1} path={2} sync_branch={3}" -f $p.Name, $(if ($repo) { $repo } else { '-'}), $(if ($path) { $path } else { '-'}), $(if ($branch) { $branch } else { '-'}))
  }
}

function Invoke-AddProject([string[]]$argv) {
  if ($argv.Count -lt 2 -or $argv.Count -gt 3) { throw 'usage: add-project <owner/repo> <fixture-path> [sync_branch]' }
  $spec = Test-RepoSpec $argv[0]
  $path = Resolve-FixturePath $argv[1]
  $branch = if ($argv.Count -eq 3) { Test-SyncBranch $argv[2] } else { $null }
  $lock = Acquire-AdminLock
  $backup = $null
  try {
    $backup = Backup-Config 'add-project'
    $payload = ConvertTo-JsonLine @{ name = $spec.Name; path = $path; repo = $spec.Url; sync_branch = $branch }
    Edit-ProjectsOnly {
      param($py)
      $payload | & (Get-PythonExe) $py $Config add
    }
    Write-Audit 'add-project' $true "added $($spec.Spec)" $backup @{ repo = $spec.Spec; path = $path; sync_branch = $branch }
    Write-Output "ok=yes op=add-project project=$($spec.Name) repo=$($spec.Spec) path=$path backup=$backup"
    Write-Output "note=running launcher will not see this entry until restart-idle"
  } catch {
    Write-Audit 'add-project' $false ([string]$_.Exception.Message) $backup @{ repo = $argv[0] }
    throw
  } finally {
    $lock.Close()
  }
}

function Find-ProjectName([string]$specName, $projects) {
  if ($projects.PSObject.Properties[$specName]) { return $specName }
  $want = "https://github.com/mojomast/$specName.git"
  foreach ($p in $projects.PSObject.Properties) {
    if ($p.Value.PSObject.Properties['repo'] -and [string]$p.Value.repo -eq $want) { return $p.Name }
  }
  return $null
}

function Invoke-RemoveProject([string[]]$argv) {
  if ($argv.Count -ne 1) { throw 'usage: remove-project <owner/repo>' }
  $spec = Test-RepoSpec $argv[0]
  $lock = Acquire-AdminLock
  $backup = $null
  try {
    $cfg = Get-Content -LiteralPath $Config -Raw -Encoding utf8 | ConvertFrom-Json
    $name = Find-ProjectName $spec.Name $cfg.projects
    if (-not $name) { throw "project not found: $($spec.Spec)" }
    $fixture = $null
    if ($cfg.projects.$name.PSObject.Properties['path']) { $fixture = [string]$cfg.projects.$name.path }
    $backup = Backup-Config 'remove-project'
    $payload = ConvertTo-JsonLine @{ name = $name }
    Edit-ProjectsOnly {
      param($py)
      $payload | & (Get-PythonExe) $py $Config remove
    }
    Write-Audit 'remove-project' $true "removed $name (fixture kept)" $backup @{ repo = $spec.Spec; fixture_kept = $fixture }
    Write-Output "ok=yes op=remove-project project=$name repo=$($spec.Spec) backup=$backup"
    Write-Output "fixture_kept=$(if ($fixture) { $fixture } else { 'none' })"
    Write-Output "note=fixture directory was not deleted"
  } catch {
    Write-Audit 'remove-project' $false ([string]$_.Exception.Message) $backup @{ repo = $argv[0] }
    throw
  } finally {
    $lock.Close()
  }
}

function Invoke-StartDetached {
  # start.ps1 already detaches uv; it also calls status.ps1, whose errors may include a bind address.
  $out = & "$ScriptDir\start.ps1" 2>&1 | Out-String
  Write-Scrubbed $out
  if ($LASTEXITCODE -ne 0) { throw "start.ps1 exited $LASTEXITCODE" }
}

function Invoke-RestartIdle {
  $lock = Acquire-AdminLock
  try { Invoke-RestartIdleCore } finally { $lock.Close() }
}

# Caller must hold the admin lock (FileShare.None, so re-acquiring here would self-deadlock).
function Invoke-RestartIdleCore {
  $backup = $null
  try {
    $backup = Backup-Config 'restart-idle'
    $active = Get-ActiveBeeCount
    Write-Output "bee_active_before=$active"
    $deadline = (Get-Date).AddSeconds($RestartWaitSeconds)
    while ($active -gt 0) {
      if ((Get-Date) -ge $deadline) {
        Write-Audit 'restart-idle' $false "timeout waiting for 0 bees ($active still active); cancelled nothing" $backup @{ bee_active = $active }
        throw "timeout: $active bee(s) still running after $RestartWaitSeconds s; cancelled nothing"
      }
      Start-Sleep -Seconds 5
      $active = Get-ActiveBeeCount
    }
    Write-Output "bee_active_ready=0"
    $stopOut = & "$ScriptDir\stop.ps1" 2>&1 | Out-String
    Write-Scrubbed $stopOut
    if ($LASTEXITCODE -ne 0) { throw "stop.ps1 exited $LASTEXITCODE" }
    try {
      Invoke-StartDetached
      $code = Wait-LauncherHealthy
      Write-Audit 'restart-idle' $true "restarted, health_http=$code" $backup @{ bee_active_before = 0 }
      Write-Output "ok=yes op=restart-idle health_http=$code backup=$backup"
    } catch {
      Write-Output "start_or_health_failed; restoring backup and restarting"
      if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath $backup -Destination $Config -Force }
      & "$ScriptDir\stop.ps1" 2>&1 | Out-String | ForEach-Object { Write-Scrubbed $_ } | Out-Null
      try { Invoke-StartDetached; [void](Wait-LauncherHealthy) } catch { }
      Write-Audit 'restart-idle' $false ("failed, attempted restore+restart: " + $_.Exception.Message) $backup @{}
      throw
    }
  } catch {
    if ($_.Exception.Message -notmatch 'timeout:|failed, attempted') {
      Write-Audit 'restart-idle' $false ([string]$_.Exception.Message) $backup @{}
    }
    throw
  }
}

# ---------------- logs ----------------
function Test-NotReparsePoint([string]$path) {
  $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw "refusing reparse point (junction/symlink): $(Split-Path -Leaf $path)"
  }
}

function Resolve-AttemptLogPath([string]$attemptId) {
  if ([string]::IsNullOrEmpty($attemptId) -or $attemptId -cnotmatch $AttemptIdPattern) {
    throw 'attempt id must match ^att_[0-9a-f]{8,32}$'
  }
  $sep = [System.IO.Path]::DirectorySeparatorChar
  $base = [System.IO.Path]::GetFullPath($AttemptsDir).TrimEnd($sep) + $sep
  if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw 'attempts directory not found' }
  # The data dir and the attempts dir themselves must be real directories, not junctions.
  Test-NotReparsePoint $Data
  Test-NotReparsePoint $base.TrimEnd($sep)
  $full = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($base, "$attemptId.events.jsonl"))
  if (-not $full.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'resolved log path escapes the attempts directory'
  }
  if ((Split-Path -Parent $full).TrimEnd($sep) + $sep -ne $base) {
    throw 'resolved log path is not directly inside the attempts directory'
  }
  if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "no log for $attemptId" }
  Test-NotReparsePoint $full
  return $full
}

function Get-ClampedLineCount($raw) {
  if ($null -eq $raw -or [string]$raw -eq '') { return $LogsDefaultLines }
  if ([string]$raw -notmatch '^[0-9]{1,7}$') { throw 'lines must be a positive integer' }
  $n = [int64]$raw
  if ($n -lt 1) { return 1 }
  if ($n -gt $LogsMaxLines) { return $LogsMaxLines }
  return [int]$n
}

function Invoke-Logs([string[]]$argv) {
  if ($argv.Count -lt 1 -or $argv.Count -gt 2) { throw 'usage: logs <attempt-id> [lines]' }
  $path = Resolve-AttemptLogPath $argv[0]
  $n = Get-ClampedLineCount $(if ($argv.Count -eq 2) { $argv[1] } else { $null })
  Write-Output "ok=yes op=logs attempt=$($argv[0]) lines=$n"
  foreach ($l in @(Get-Content -LiteralPath $path -Tail $n -Encoding utf8)) {
    Write-Output (Format-RedactedLogLine $l)
  }
}

# ---------------- apply-update ----------------
function Invoke-Git([string[]]$gitArgs, [switch]$AllowFail) {
  # Fixed git invocation: no hooks, no fsmonitor, no file:// transports, no credential prompts.
  $base = @('-C', $HyvvCheckout, '-c', 'core.hooksPath=NUL', '-c', 'core.fsmonitor=false',
            '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never', '-c', 'submodule.recurse=false')
  $env:GIT_TERMINAL_PROMPT = '0'
  # git fetch writes "From <url>" to stderr even on success; with $ErrorActionPreference=Stop
  # that becomes a NativeCommandError and apply-update refuses before the exit-code check.
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & git @base @gitArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prevEap
  }
  if (-not $AllowFail -and $code -ne 0) { throw "git $($gitArgs[0]) failed (exit $code): $((Write-Scrubbed $out) -join ' ')" }
  return [pscustomobject]@{ Code = $code; Out = $out.Trim() }
}

function Get-JournalSnapshot {
  # Strict read of the launcher's own attempt journal (attempts\<id>.json, written atomically by
  # launcher.py via tmp + os.replace on every state change). Any fact we cannot establish -> throw.
  if (-not (Test-Path -LiteralPath $AttemptsDir -PathType Container)) { throw 'attempts journal directory missing' }
  Test-NotReparsePoint $Data
  Test-NotReparsePoint $AttemptsDir
  $tmp = @(Get-ChildItem -LiteralPath $AttemptsDir -Filter '*.tmp' -File -Force -ErrorAction Stop)
  if ($tmp.Count) { throw "journal write in progress ($($tmp.Count) .tmp file(s))" }
  $busy = 0; $total = 0
  foreach ($f in @(Get-ChildItem -LiteralPath $AttemptsDir -Filter '*.json' -File -Force -ErrorAction Stop)) {
    if ($f.Name -cnotmatch '^att_[0-9a-f]{8,32}\.json$') { continue }   # sidecars (.result.json etc.)
    if ($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw "journal entry is a reparse point: $($f.Name)" }
    try { $a = Get-Content -LiteralPath $f.FullName -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "unreadable journal entry $($f.Name)" }
    if ($null -eq $a -or -not $a.PSObject.Properties['state']) { throw "journal entry without state: $($f.Name)" }
    $st = [string]$a.state
    if ($KnownAttemptStates -cnotcontains $st) { throw "unknown attempt state '$st' in $($f.Name)" }
    $total++
    if ($BeeBusyStates -ccontains $st) { $busy++ }
  }
  return [pscustomobject]@{ Busy = $busy; Total = $total }
}

function Assert-HostIdle {
  # Fail closed: two strict journal reads a few seconds apart must both show zero busy attempts
  # and the same entry count (no attempt created in between). No token, no /v1 call.
  $a = Get-JournalSnapshot
  if ($a.Busy -ne 0) { throw "host busy (journal_busy=$($a.Busy)); zero required, nothing changed" }
  Start-Sleep -Seconds $IdleRecheckSeconds
  $b = Get-JournalSnapshot
  if ($b.Busy -ne 0 -or $b.Total -ne $a.Total) { throw "journal changed during idle check (busy=$($b.Busy), entries $($a.Total)->$($b.Total)); refusing" }
  return $b
}

function Get-LoopbackServerVersion {
  # Unauthenticated GET: the launcher answers 401, and BaseHTTPRequestHandler sends
  # 'Server: orbit-launcher/<VERSION> Python/x.y'. That proves the process is listening and which
  # launcher.py VERSION it loaded, without any credential.
  $port = Get-LauncherPort
  $code = $null; $server = $null
  try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/health" -Method GET -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    $code = [int]$resp.StatusCode; $server = [string]$resp.Headers['Server']
  } catch {
    $r = $_.Exception.Response
    if ($r) {
      $code = [int]$r.StatusCode
      try { $server = [string]$r.Headers['Server'] } catch { }                       # WebResponse (PS 5.1)
      if (-not $server) { try { $server = ($r.Headers.Server | ForEach-Object { $_.ToString() }) -join ' ' } catch { } }  # HttpResponseMessage (PS 7)
    }
  }
  $ver = $null
  if ($server -match 'orbit-launcher/([0-9A-Za-z.+-]+)') { $ver = $Matches[1] }
  return [pscustomobject]@{ Code = $code; Version = $ver }
}

function Get-FileSha256([string]$path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }

function Write-DeployedVersion([string]$sha, [string]$version) {
  $files = [ordered]@{}
  foreach ($rel in $InstallPayload) { $files[$rel] = Get-FileSha256 (Join-Path $Root $rel) }
  $rec = [ordered]@{ repo = 'mojomast/hyvv'; sha = $sha; version = $version; installed_at_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); files = $files }
  $dest = Join-Path $Root $DeployedVersionFile
  $tmp = "$dest.tmp-admin"
  Set-Content -LiteralPath $tmp -Value ($rec | ConvertTo-Json -Depth 4) -Encoding utf8
  Move-Item -LiteralPath $tmp -Destination $dest -Force
}

function Get-DeployedVersion {
  # Never throws: absent/unreadable/stale file -> status 'unknown / mismatch'.
  $p = Join-Path $Root $DeployedVersionFile
  $out = [ordered]@{ status = 'unknown / mismatch'; sha = $null; version = $null; installed_at_utc = $null }
  if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return [pscustomobject]$out }
  try {
    $rawJson = Get-Content -LiteralPath $p -Raw -Encoding utf8
    $d = $rawJson | ConvertFrom-Json
    $out.sha = [string]$d.sha; $out.version = [string]$d.version
    # Read the timestamp textually: PS 7's ConvertFrom-Json turns ISO strings into local DateTime.
    if ($rawJson -match '"installed_at_utc"\s*:\s*"([0-9TZ:-]+)"') { $out.installed_at_utc = $Matches[1] }
    $match = $true
    foreach ($rel in $InstallPayload) {
      $f = Join-Path $Root $rel
      $want = $null; if ($d.files -and $d.files.PSObject.Properties[$rel]) { $want = [string]$d.files.$rel }
      if (-not $want -or -not (Test-Path -LiteralPath $f -PathType Leaf) -or (Get-FileSha256 $f) -ne $want) { $match = $false }
    }
    if ($match -and $out.sha -match '^[0-9a-f]{40}$') { $out.status = 'match' }
  } catch { }
  return [pscustomobject]$out
}

function Test-LauncherRunsFromRoot {
  $want = [System.IO.Path]::GetFullPath((Join-Path $Root 'launcher.py'))
  return [bool](@(Get-LauncherProcesses | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($want, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count)
}

function Get-PayloadVersion([string]$dir) {
  $py = Join-Path $dir 'launcher.py'
  $m = Select-String -LiteralPath $py -Pattern '^VERSION\s*=\s*"([0-9A-Za-z.+-]+)"' | Select-Object -First 1
  if (-not $m) { throw 'could not read VERSION from launcher.py' }
  return $m.Matches[0].Groups[1].Value
}

function Backup-Payload {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $dir = Join-Path $Data ("payload.bak-apply-update-{0}" -f $stamp)
  New-Item -ItemType Directory -Force $dir | Out-Null
  foreach ($rel in @($InstallPayload + $DeployedVersionFile)) {
    $src = Join-Path $Root $rel
    if (Test-Path -LiteralPath $src -PathType Leaf) { Copy-Item -LiteralPath $src -Destination (Join-Path $dir $rel) -Force }
  }
  return $dir
}

function Restore-Payload([string]$dir) {
  foreach ($rel in $InstallPayload) {
    $b = Join-Path $dir $rel
    if (Test-Path -LiteralPath $b -PathType Leaf) { Copy-Item -LiteralPath $b -Destination (Join-Path $Root $rel) -Force }
  }
  $vb = Join-Path $dir $DeployedVersionFile; $vl = Join-Path $Root $DeployedVersionFile
  if (Test-Path -LiteralPath $vb -PathType Leaf) { Copy-Item -LiteralPath $vb -Destination $vl -Force }
  elseif (Test-Path -LiteralPath $vl) { Remove-Item -LiteralPath $vl -Force }
}

function Invoke-Install {
  # Validate everything in the checkout first, then copy, so a half-copied payload is unlikely.
  foreach ($rel in $InstallPayload) {
    $src = Join-Path $HyvvCheckout $rel
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "checkout is missing $rel" }
    Test-NotReparsePoint $src
  }
  $comp = & uv run -q --python $Python python -m py_compile (Join-Path $HyvvCheckout 'launcher.py') (Join-Path $HyvvCheckout 'agy_cua_mcp.py') 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw "py_compile failed: $((Write-Scrubbed $comp) -join ' ')" }
  foreach ($rel in $InstallPayload) {
    Copy-Item -LiteralPath (Join-Path $HyvvCheckout $rel) -Destination (Join-Path $Root $rel) -Force
  }
  $warm = & uv @InstallUvArgs 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw "uv env warm-up failed: $((Write-Scrubbed $warm) -join ' ')" }
}

function Invoke-ApplyUpdate([string[]]$argv) {
  if ($argv.Count -lt 1 -or $argv.Count -gt 2) { throw 'usage: apply-update <commit-sha> [hyvv]' }
  $ref = $argv[0]
  $repo = if ($argv.Count -eq 2) { $argv[1] } else { $HyvvRepoName }
  $auditArgs = [ordered]@{ repo = $repo; ref = $ref; old_sha = $null; new_sha = $null; version_before = $null; version_after = $null; payload_backup = $null; rolled_back = $false }
  try {
    if ($repo -cne $HyvvRepoName) { throw "repo must be '$HyvvRepoName'" }
    if ($ref -cnotmatch '^[0-9a-f]{7,40}$') { throw 'ref must be a lowercase commit sha (7-40 hex); branches and tags are refused' }
  } catch {
    Write-Audit 'apply-update' $false ("refused: " + $_.Exception.Message) $null $auditArgs
    throw
  }
  $lock = Acquire-AdminLock
  try {
    try {
      if (-not (Test-Path -LiteralPath (Join-Path $HyvvCheckout '.git'))) { throw "hyvv checkout not found at $HyvvCheckout" }
      Test-NotReparsePoint $HyvvCheckout
      $origin = (Invoke-Git @('remote','get-url','origin')).Out
      if ($origin -ne $HyvvOriginUrl) { throw 'checkout origin is not mojomast/hyvv' }
      $dirty = (Invoke-Git @('status','--porcelain','--untracked-files=all')).Out
      if ($dirty) { throw 'hyvv checkout is dirty; refusing' }
      $old = (Invoke-Git @('rev-parse','--verify','HEAD^{commit}')).Out
      $auditArgs.old_sha = $old
      [void](Invoke-Git @('fetch','--prune','--no-tags','origin','+refs/heads/main:refs/remotes/origin/main'))
      $r = Invoke-Git @('rev-parse','--verify','--quiet',"$ref^{commit}") -AllowFail
      if ($r.Code -ne 0 -or $r.Out -notmatch '^[0-9a-f]{40}$') { throw 'ref does not resolve to exactly one commit' }
      $new = $r.Out
      # A hex-looking branch/tag name could shadow a short sha: the resolved sha must start with what was asked for.
      if (-not $new.StartsWith($ref)) { throw 'ref resolved to a different object (ambiguous name); refusing' }
      $auditArgs.new_sha = $new
      $anc = Invoke-Git @('merge-base','--is-ancestor',$new,$HyvvMainRef) -AllowFail
      if ($anc.Code -ne 0) { throw 'commit is not on origin/main; refusing' }
      [void](Assert-HostIdle)
      $dv = Get-DeployedVersion
      $auditArgs.version_before = $(if ($dv.status -eq 'match') { "$($dv.version)@$($dv.sha)" } else { 'unknown / mismatch' })
    } catch {
      Write-Audit 'apply-update' $false ("refused: " + $_.Exception.Message) $null $auditArgs
      throw
    }

    if ($new -eq $old) { Write-Output "note=checkout already at $new; reinstalling and restarting" }
    $payloadBak = Backup-Payload
    $auditArgs.payload_backup = $payloadBak
    try {
      [void](Invoke-Git @('checkout','--detach','--quiet',$new))
      if ((Invoke-Git @('rev-parse','HEAD')).Out -ne $new) { throw 'checkout did not land on the requested commit' }
      $want = Get-PayloadVersion $HyvvCheckout
      Invoke-Install
      Write-DeployedVersion $new $want
      $dv1 = Get-DeployedVersion
      if ($dv1.status -ne 'match' -or $dv1.sha -ne $new) { throw 'version file did not read back as the new sha' }
      Invoke-RestartIdleCore          # waits for loopback 401/200 (unauthenticated) = process listening
      $probe = Get-LoopbackServerVersion
      if ($probe.Code -ne 401 -and $probe.Code -ne 200) { throw "launcher not listening after restart (http=$($probe.Code))" }
      if ($probe.Version -ne $want) { throw "running launcher reports version '$($probe.Version)', expected $want" }
      if (-not (Test-LauncherRunsFromRoot)) { throw 'no launcher process is running launcher.py from the install dir' }
      $auditArgs.version_after = "$want@$new"
      Write-Audit 'apply-update' $true "updated $old -> $new" $payloadBak $auditArgs
      Write-Output "ok=yes op=apply-update old_sha=$old new_sha=$new version=$want deployed=$($dv1.status) payload_backup=$payloadBak"
    } catch {
      $why = [string]$_.Exception.Message
      Write-Output "apply_update_failed; reverting to $old"
      $auditArgs.rolled_back = $true
      $revert = 'ok'
      try { [void](Invoke-Git @('checkout','--detach','--quiet',$old)) } catch { $revert = 'checkout-failed' }
      # Restore the exact bytes that were running before (they may predate the checkout).
      try { Restore-Payload $payloadBak } catch { $revert = 'restore-failed' }
      try { Invoke-RestartIdleCore } catch { $revert = "$revert,restart-failed" }
      $pr = Get-LoopbackServerVersion
      if ($pr.Code -ne 401 -and $pr.Code -ne 200) { $revert = "$revert,not-listening" }
      $dvr = Get-DeployedVersion
      $auditArgs.version_after = "running=$($pr.Version) deployed=$($dvr.status)"
      Write-Audit 'apply-update' $false ("failed: $why; revert=$revert") $payloadBak $auditArgs
      throw "apply-update failed: $why (revert=$revert, old_sha=$old)"
    }
  } finally {
    $lock.Close()
  }
}

# --- entry ---
$argsFromMain = @($args)
try {
  $argv = Get-AdminArgv
  if ($argv.Count -eq 0) { throw 'no operation given' }
  $op = $argv[0]
  $rest = @()
  if ($argv.Count -gt 1) { $rest = @($argv[1..($argv.Count-1)]) }
  if ($AllowedOps -notcontains $op) {
    throw "invalid op: $op (allowed: $($AllowedOps -join ', '))"
  }
  switch ($op) {
    'status' { Invoke-Status }
    'list-projects' { Invoke-ListProjects }
    'add-project' { Invoke-AddProject $rest }
    'remove-project' { Invoke-RemoveProject $rest }
    'restart-idle' { Invoke-RestartIdle }
    'logs' { Invoke-Logs $rest }
    'apply-update' { Invoke-ApplyUpdate $rest }
  }
  exit 0
} catch {
  Write-Scrubbed ("error: " + $_.Exception.Message)
  exit 1
}

