#!/usr/bin/env bash
# Fail if the tree looks like it contains secrets, tokens, tailnet host IPs or
# personal Windows/Unix home paths. Used in CI and before every push.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
# Capture candidate tailnet/CGNAT addresses, then drop the documented range
# prefix 100.64.0.0/10 that launcher.check_bind uses.
if rg -n --hidden --glob '!.git/**' --glob '!scripts/scan-secrets.sh' \
  -e '100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}' \
  . > "$tmp" 2>/dev/null; then
  if grep -vE '100\.64\.0\.0/10' "$tmp" | grep -q .; then
    echo "scan-secrets: possible tailnet or CGNAT host address:" >&2
    grep -vE '100\.64\.0\.0/10' "$tmp" >&2
    exit 1
  fi
fi

if rg -n --hidden --glob '!.git/**' --glob '!scripts/scan-secrets.sh' \
  -e '\.ts\.net\b' \
  -e 'C:\\Users\\kyle\b' \
  -e '/Users/kyle\b' \
  -e '/home/kyle\b' \
  -e 'ghp_[A-Za-z0-9]{20,}' \
  -e 'gho_[A-Za-z0-9]{20,}' \
  -e 'sk-[A-Za-z0-9]{20,}' \
  . ; then
  echo "scan-secrets: matched a disallowed pattern" >&2
  exit 1
fi
exit 0
