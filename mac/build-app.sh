#!/usr/bin/env bash
# Builds a self-contained "GitHub Helper.app" (with icon) into ./dist (or the folder given as $1).
cd "$(dirname "$0")/.." || exit 1
command -v python3 >/dev/null 2>&1 || { echo "python3 is required (run: xcode-select --install)" >&2; exit 1; }
python3 linux/server.py --build-app "${1:-dist}"
