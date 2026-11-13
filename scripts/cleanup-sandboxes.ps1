# Deletes leftover launcher sandboxes (never touches others).
# Names: legacy orbit-<12 hex>, or <model abbr>-<slug>-<10-char attempt id> (launcher.sandbox_name; abbrs = launcher.MODEL_ABBR).
. "$PSScriptRoot\common.ps1"
$pat = 'local:((?:orbit-[0-9a-f]{12})|(?:(?:mimo26fr|nm35svfr|dsflash|glm53fr|nm35lfr|beamfr|ds41fr|ds41go|lc25fr|mm31fr|co55h|co55l|co55m|cs55h|cs55l|cs55m|gf38h|gf38l|gf38m|bpkl|ds41|ga4h|ga4l|ga4m|ga4)-(?:[a-z0-9]+-)*[a-z0-9]{10}))\b'
$ls = Invoke-Cua sb ls 2>&1 | Out-String
[regex]::Matches($ls,$pat) | % { $_.Groups[1].Value } | Sort -Unique | % { "rm $_ : " + ((Invoke-Cua sb rm -f $_ 2>&1 | Out-String).Trim()) }
Invoke-Cua sb ls
