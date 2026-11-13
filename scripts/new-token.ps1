# Creates the per-launcher API token Orbit uses. Copy it to Orbit with scripts\show-token-once.ps1 only when asked.
. "$PSScriptRoot\common.ps1"
$f = Join-Path $Data 'orbit-api.token'
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
Set-Content -Path $f -Value ([BitConverter]::ToString($b) -replace '-','').ToLower() -NoNewline -Encoding ascii
Lock-UserOnly $f
Write-Host "Token written to $f (not shown)."
