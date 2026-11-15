@echo off
rem Starts GitHub Helper in a hidden PowerShell window and closes this console right away.
rem To see the server output run: powershell -NoProfile -STA -ExecutionPolicy Bypass -File server.ps1
start "" powershell -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0server.ps1"
