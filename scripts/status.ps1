. "$PSScriptRoot\common.ps1"
$c = Get-Content $Config | ConvertFrom-Json
$t = (Get-Content (Join-Path $Data 'orbit-api.token') -Raw).Trim()
try { Invoke-RestMethod "http://$($c.bind):$($c.port)/v1/health" -Headers @{Authorization="Bearer $t"} | Format-List } catch { Write-Host "Launcher not answering: $($_.Exception.Message)"; Get-Content "$Log.err" -Tail 20 -EA 0 }
