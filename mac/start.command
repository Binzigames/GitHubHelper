#!/usr/bin/env bash
# GitHub Helper launcher for macOS (double-click in Finder, or run ./start.command)
cd "$(dirname "$0")/.." || exit 1
exec ./linux/start.sh "$@"
