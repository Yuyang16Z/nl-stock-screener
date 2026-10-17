#!/usr/bin/env bash
# 打包笔试提交附件：git 已提交的源码（不含 .env、缓存、node_modules）+ 提交说明。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/../第一题_ScreenLens_提交.zip}"
TMP="$(mktemp -d)"
mkdir -p "$TMP/ScreenLens"
git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$TMP/ScreenLens"
cp "$ROOT/docs/SUBMISSION.md" "$TMP/SUBMISSION.md"
rm -f "$OUT"
(cd "$TMP" && zip -qr "$OUT" .)
rm -rf "$TMP"
ls -lh "$OUT"
unzip -l "$OUT" | tail -1
