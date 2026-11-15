:; cd "$(dirname "$0")" && exec ./linux/start.sh "$@"
@echo off
call "%~dp0windows\start.cmd"
