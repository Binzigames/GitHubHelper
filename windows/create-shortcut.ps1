# Creates "GitHub Helper" shortcuts (with the app icon) that start the app without a console window.
#   Start Menu entry   - always
#   Desktop shortcut   - with -Desktop
#   -Quiet             - no output (used by the app itself)
param([switch]$Desktop, [switch]$Quiet)
$ErrorActionPreference = 'Stop'
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = Split-Path -Parent $Root
$Server  = Join-Path $Root 'server.ps1'
$Icon    = Join-Path $AppRoot 'assets\icon.ico'

function New-Shortcut([string]$lnk) {
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $s.Arguments = '-NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $Server + '"'
    $s.WorkingDirectory = $AppRoot
    $s.WindowStyle = 7   # minimized: no console flash
    $s.Description = 'GitHub Helper - commits, branches and GitHub sync without the command line'
    if (Test-Path -LiteralPath $Icon) { $s.IconLocation = $Icon + ',0' }
    $s.Save()
    if (-not $Quiet) { Write-Host "Created $lnk" }
}

$start = Join-Path ([Environment]::GetFolderPath('Programs')) 'GitHub Helper.lnk'
New-Shortcut $start
if ($Desktop) { New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'GitHub Helper.lnk') }
