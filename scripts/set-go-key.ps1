# Stores the OpenCode Go key in a user-only file. Reads it hidden; never echoes it.
. "$PSScriptRoot\common.ps1"
$s = Read-Host 'Paste OpenCode Go API key (hidden)' -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
$f = Join-Path $Data 'go.key'
Set-Content -Path $f -Value $plain -NoNewline -Encoding ascii; $plain = $null
Lock-UserOnly $f
Write-Host "Saved to $f (readable by $env:USERNAME only)."
