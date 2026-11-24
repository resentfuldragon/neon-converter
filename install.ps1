<#
.SYNOPSIS
  Installs a built Neon Converter for the current user (no admin needed):
  copies it to %LOCALAPPDATA%\Programs\Neon Converter and adds Start menu,
  Desktop and right-click "Send to" shortcuts.

.EXAMPLE
  .\install.ps1                          # installs from .\dist\NeonConverter
  .\install.ps1 -Source D:\NeonConverter # installs from an unzipped release
  .\install.ps1 -Uninstall
#>
param(
    [string]$Source = (Join-Path $PSScriptRoot "dist\NeonConverter"),
    [switch]$NoDesktop,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

$target = Join-Path $env:LOCALAPPDATA "Programs\Neon Converter"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Neon Converter.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Neon Converter.lnk"
$sendTo = Join-Path $env:APPDATA "Microsoft\Windows\SendTo\Neon Converter.lnk"

if ($Uninstall) {
    foreach ($p in @($startMenu, $desktop, $sendTo)) { if (Test-Path $p) { Remove-Item $p } }
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Write-Host "Neon Converter removed." -ForegroundColor Magenta
    return
}

if (-not (Test-Path (Join-Path $Source "NeonConverter.exe"))) {
    throw "NeonConverter.exe not found in '$Source'. Build first with .\build.ps1 or pass -Source."
}

Get-Process NeonConverter -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
Copy-Item $Source $target -Recurse
$exe = Join-Path $target "NeonConverter.exe"

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $target
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "Convert documents, e-books, images, video and audio"
    $lnk.Save()
}
New-Shortcut $startMenu
New-Shortcut $sendTo
if (-not $NoDesktop) { New-Shortcut $desktop }

Write-Host "Installed to $target" -ForegroundColor Cyan
Write-Host "Shortcuts: Start menu, Desktop, and right-click > Send to > Neon Converter" -ForegroundColor Cyan
