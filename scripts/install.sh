#!/bin/sh
# Install or update hyvv on Linux or macOS with uv.
# Default: run as the logged-in user. A system service is opt-in (--mode system).
# Does not overwrite config.json, *.token or *.key, and never prints secret values.
#
#   ./scripts/install.sh --dry-run
#   ./scripts/install.sh
#   ./scripts/install.sh --mode system   # opt-in; may need admin for linger / units
set -eu

mode=user
dry_run=0
prefix="${HYVV_PREFIX:-}"
version="0.1.16"
source_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"

usage() {
  cat <<USAGE
hyvv install (Linux and macOS)

  $0 [--dry-run] [--mode user|system] [--prefix DIR] [--version VER]

Default mode is user (logged-in account). --mode system is opt-in for
headless hosts and is not fully wired in 0.1.15 (the files are copied;
the unit/plist is planned).

Data dir (compat): \$HYVV_DATA or ~/.orbit-launcher
Config is never overwritten if it already exists.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dry-run) dry_run=1 ;;
    --mode) mode="${2:-}"; shift ;;
    --prefix) prefix="${2:-}"; shift ;;
    --version) version="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

uname_s="$(uname -s)"
case "$uname_s" in
  Linux|Darwin) ;;
  *) echo "unsupported OS: $uname_s (use scripts/install.ps1 on Windows)" >&2; exit 2 ;;
esac

if [ -z "$prefix" ]; then
  case "$mode" in
    user)
      if [ "$uname_s" = Darwin ]; then
        prefix="${HOME}/Library/Application Support/hyvv"
      else
        prefix="${XDG_DATA_HOME:-$HOME/.local/share}/hyvv"
      fi
      ;;
    system)
      prefix="/opt/hyvv"
      ;;
    *) echo "mode must be user or system" >&2; exit 2 ;;
  esac
fi

data="${HYVV_DATA:-$HOME/.orbit-launcher}"
dest="$prefix/versions/$version"

echo "hyvv install version=$version mode=$mode os=$uname_s prefix=$prefix"
if [ "$dry_run" -eq 1 ]; then
  echo "dry-run: would install into $dest with uv, leave $data/config.json untouched if present"
  command -v uv >/dev/null 2>&1 && uv --version || echo "uv: not on PATH (would be installed, pinned, if missing)"
  command -v python3 >/dev/null 2>&1 && python3 --version || echo "python3: missing (uv would bring its own)"
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 2
fi

mkdir -p "$dest" "$data"
# Install this checkout (or a later released wheel) into a versioned venv.
uv venv --python 3.12 "$dest/.venv"
# Prefer a local checkout; fall back to PyPI once a release exists.
if [ -f "$source_root/pyproject.toml" ]; then
  uv pip install --python "$dest/.venv/bin/python" "$source_root"
else
  uv pip install --python "$dest/.venv/bin/python" "hyvv==$version"
fi

# current pointer
ln -sfn "$dest" "$prefix/current"
# user bin shim
bindir="$HOME/.local/bin"
mkdir -p "$bindir"
ln -sfn "$prefix/current/.venv/bin/hyvv" "$bindir/hyvv"

cfg="$data/config.json"
if [ ! -f "$cfg" ]; then
  example="$source_root/config.example.json"
  if [ ! -f "$example" ]; then
    example="$dest/.venv/lib/python3.12/site-packages/hyvv/config.example.json"
  fi
  if [ -f "$example" ]; then
    cp "$example" "$cfg"
    echo "Created $cfg with bind 127.0.0.1. Edit projects and bind, then start hyvv. Existing keys and tokens were not touched."
  fi
else
  echo "Left existing config, keys and tokens unchanged."
fi

if [ "$mode" = system ]; then
  echo "system mode is opt-in and planned: files are in $prefix; systemd/launchd units ship in a later 0.1.x."
fi

echo "Installed hyvv $version to $dest (user-level default)."
