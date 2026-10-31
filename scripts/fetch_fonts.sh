#!/usr/bin/env bash
# Downloads the self-hosted fonts (Inter, Sora, Noto Sans Ethiopic - all SIL OFL) into src/static/fonts
# so the app never depends on Google Fonts or on what a phone happens to have installed.
# Run by deploy_azure.sh and CI before the image build. Safe to re-run; existing files are kept.
set -u
cd "$(dirname "$0")/.."
DEST=src/static/fonts; mkdir -p "$DEST"
BASE=https://cdn.jsdelivr.net/fontsource/fonts
get() { # get <url> <file>
  [ -s "$DEST/$2" ] && return 0
  python3 - "$1" "$DEST/$2" <<'PY' || { echo "WARNING: could not fetch $2 (the app falls back to system fonts)"; return 0; }
import sys, urllib.request
data = urllib.request.urlopen(sys.argv[1], timeout=30).read()
assert data[:4] == b"wOF2", "not a woff2 file"
open(sys.argv[2], "wb").write(data)
PY
}
get "$BASE/inter:vf@latest/latin-wght-normal.woff2"            inter-latin-wght.woff2
get "$BASE/sora:vf@latest/latin-wght-normal.woff2"             sora-latin-wght.woff2
get "$BASE/noto-sans-ethiopic@latest/ethiopic-400-normal.woff2" noto-ethiopic-400.woff2
get "$BASE/noto-sans-ethiopic@latest/ethiopic-700-normal.woff2" noto-ethiopic-700.woff2
ls -la "$DEST" | tail -n +2
