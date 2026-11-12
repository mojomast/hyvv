# One-time agy setup for the launcher's agy-cua kind. Does NOT touch the running launcher.
# Registers the orbit-cua MCP shim and makes mcp(orbit-cua/*) agy's ONLY allowed tool rule.
# Backs up both files to *.bak-agycua first (once).
. "$PSScriptRoot\common.ps1"
$agy = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
$uvExe = (Get-Command uv).Source
$shim = Join-Path $Root 'agy_cua_mcp.py'
$mcp = Join-Path $env:USERPROFILE '.gemini\config\mcp_config.json'
$set = Join-Path $env:USERPROFILE '.gemini\antigravity-cli\settings.json'
foreach ($f in @($mcp, $set)) { if ((Test-Path $f) -and -not (Test-Path "$f.bak-agycua")) { Copy-Item $f "$f.bak-agycua" } }
& $agy mcp add orbit-cua '--' $uvExe run -q --python 3.12 --with cua==0.5.0 python $shim
$j = if (Test-Path $set) { Get-Content $set -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $j.PSObject.Properties['permissions']) { $j | Add-Member permissions ([pscustomobject]@{}) }
$j.permissions | Add-Member -Force allow @('mcp(orbit-cua/*)')
# agy ignores a settings.json that starts with a UTF-8 BOM (PS 5.1 Set-Content -Encoding utf8 writes one): write BOM-less.
[System.IO.File]::WriteAllText($set, ($j | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
& $agy mcp list
Get-Content $set
