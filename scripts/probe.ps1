# Confirms a model answers INSIDE a real worker sandbox (creates and deletes one sandbox).
# .\probe.ps1                -> default model (deepseek-v4.1-flash via OpenCode Go)
# .\probe.ps1 -Model deepseek-v4.1-flash-free -> one free model (no key)
# .\probe.ps1 -Model free-all -> every free model in the pool, in parallel sandboxes
param([string]$Model = '')
. "$PSScriptRoot\common.ps1"
$extra = @(); if ($Model) { $extra = @('--model', $Model) }
uv run --python 3.12 --with cua==0.5.0 "$Root\launcher.py" probe --config $Config @extra
