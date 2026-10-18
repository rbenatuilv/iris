#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
OUT=dist/iris-install.sh
mkdir -p dist
cp deploy/install.sh "$OUT"
tar --exclude=__pycache__ -czf - iris config/defaults.yaml requirements.txt deploy/iris-update.sh | base64 >> "$OUT"
chmod +x "$OUT"
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
