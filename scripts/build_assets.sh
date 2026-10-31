#!/usr/bin/env bash
# Minifies src/static/js/*.js and src/static/css/*.css IN PLACE, overwriting
# the readable source with a minified build - meant to run once, during the
# Docker build (see the Dockerfile's "assets" build stage), never in local
# dev. Keeping the repo's own source files fully readable/commented is
# deliberate (that's what you're editing and reviewing); minifying only the
# deployed copy is what actually matters for "don't hand out a free guide to
# the app's internals through view-source" without hurting maintainability.
#
# This does NOT hide anything sensitive - there was nothing sensitive to
# hide (see the security review that came with this change) - it just makes
# the shipped bundle unreadable-but-functional instead of a nicely
# commented tutorial, which is the honest, actually-effective version of
# "protect the source" for client-side code.
set -euo pipefail
cd "$(dirname "$0")/.."

STATIC_DIR="src/static"

echo "Minifying JS..."
for f in "$STATIC_DIR"/js/*.js; do
  npx esbuild "$f" --minify --legal-comments=none --outfile="$f.min" --log-level=warning
  mv "$f.min" "$f"
  echo "  $f"
done

echo "Minifying CSS..."
for f in "$STATIC_DIR"/css/*.css; do
  npx esbuild "$f" --minify --outfile="$f.min" --log-level=warning
  mv "$f.min" "$f"
  echo "  $f"
done

echo "Done."
