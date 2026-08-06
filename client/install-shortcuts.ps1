# BareNOC Chat — creates Start Menu + Desktop shortcuts for the installed launcher.
# Called by install.bat:  powershell -File install-shortcuts.ps1 -AppDir "%LOCALAPPDATA%\BareNOC"
param([Parameter(Mandatory=$true)][string]$AppDir)

$ErrorActionPreference = 'Continue'
$ws = New-Object -ComObject WScript.Shell
$launcher = Join-Path $AppDir 'barenoc-chat.bat'
$icon     = Join-Path $AppDir 'barenoc-chat.ico'
$desc     = 'Legacy AIM-style chat client for the BareNOC queue manager'

if (-not (Test-Path $launcher)) {
    Write-Host '[ERROR] launcher not found:' $launcher
    exit 1
}

$startDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
if (Test-Path $startDir) {
    $sc = $ws.CreateShortcut((Join-Path $startDir 'BareNOC Chat.lnk'))
    $sc.TargetPath       = $launcher
    $sc.WorkingDirectory = $AppDir
    $sc.IconLocation     = "$icon,0"
    $sc.Description      = $desc
    $sc.Save()
    Write-Host '  + Start Menu shortcut'
}

$desktop = [Environment]::GetFolderPath('Desktop')
if (Test-Path $desktop) {
    $sc = $ws.CreateShortcut((Join-Path $desktop 'BareNOC Chat.lnk'))
    $sc.TargetPath       = $launcher
    $sc.WorkingDirectory = $AppDir
    $sc.IconLocation     = "$icon,0"
    $sc.Description      = $desc
    $sc.Save()
    Write-Host '  + Desktop shortcut'
}
