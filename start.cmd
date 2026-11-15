@echo off
title GitHub Helper
powershell -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0server.ps1"
pause
