param([switch]$DryRun)
# Deletes leftover launcher sandboxes (never touches others, never a live attempt's).
# Names: legacy orbit-<12 hex>, or <model abbr>-<slug>-<10-char attempt id> (launcher.sandbox_name; abbrs = launcher.MODEL_ABBR,
# plus config-only models whose abbr is derived, e.g. k31pro and kimiforc for the Kimi entries).
# -DryRun lists what would be removed and removes nothing.
. "$PSScriptRoot\common.ps1"
$pat = 'local:((?:orbit-[0-9a-f]{12})|(?:(?:kimiforc|hy4nous|lc25nfr|ds1017n|hy4nfr|mimo26fr|nm35svfr|dsflash|glm53fr|nm35lfr|beamfr|ds41fr|ds41go|lc25fr|mm31fr|co55h|co55l|co55m|cs55h|cs55l|cs55m|gf38h|gf38l|gf38m|bpkl|k31pro|ds41|zoo53|ga4h|ga4l|ga4m|ga4)-(?:[a-z0-9]+-)*[a-z0-9]{10}))\b'
# Sandboxes still named by a non-terminal attempt journal belong to a live bee: keep them.
# An unreadable journal could be a live one, so it fails closed: nothing is removed.
$live = @{}; $unreadable = 0
Get-ChildItem (Join-Path $Data 'attempts') -Filter *.json -EA 0 | Where-Object { $_.Name -notlike '*.result.json' } | ForEach-Object {
  try { $j = Get-Content $_.FullName -Raw | ConvertFrom-Json } catch { $unreadable++; return }
  if ($j.sandbox -and @('succeeded', 'failed', 'cancelled') -notcontains $j.state) { $live[[string]$j.sandbox] = $j.state }
}
if ($unreadable -gt 0 -and -not $DryRun) { Write-Warning "$unreadable attempt journal(s) unreadable; refusing to remove anything"; $DryRun = $true }
$ls = Invoke-Cua sb ls 2>&1 | Out-String
[regex]::Matches($ls,$pat) | % { $_.Groups[1].Value } | Sort -Unique | % {
  if ($live.ContainsKey($_)) { "keep (live attempt) $_ [$($live[$_])]" }
  elseif ($DryRun) { "would rm $_" }
  else { "rm $_ : " + ((Invoke-Cua sb rm -f $_ 2>&1 | Out-String).Trim()) }
}
if (-not $DryRun) { Invoke-Cua sb ls }
