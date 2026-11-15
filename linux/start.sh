#!/usr/bin/env bash
# GitHub Helper launcher for Linux
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found. Install it (e.g. 'sudo apt install python3') and try again." >&2
    exit 1
fi
exec python3 server.py "$@"
