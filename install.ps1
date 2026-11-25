<#
.SYNOPSIS
  Installs Neon Converter for the current Windows user - no admin rights needed.

  Copies the app to %LOCALAPPDATA%\Programs\Neon Converter, adds Start menu, Desktop and
  right-click "Send to" shortcuts, and lists it under Settings > Apps so it can be
  uninstalled from there. Installing over an older version keeps your settings.

  From the release zip you don't run this file directly: unzip the download and
  double-click "Install" (Install.cmd) next to NeonConverter.exe. This script ships inside
  the app as _internal\install.ps1.

.EXAMPLE
  .\install.ps1                          # from the repo: installs .\dist\NeonConverter
  .\install.ps1 -Source D:\NeonConverter # installs an unzipped release folder
  .\install.ps1 -Uninstall               # removes the app, its shortcuts and its settings
#>
param(
    [string]$Source = "",
    [switch]$NoDesktop,
    [switch]$NoLaunch,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
trap {
    Write-Host ""
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$target = Join-Path $env:LOCALAPPDATA "Programs\Neon Converter"
$exe = Join-Path $target "NeonConverter.exe"
$installedScript = Join-Path $target "_internal\install.ps1"
$dataDir = Join-Path $env:LOCALAPPDATA "NeonConverter"          # settings, log, engine caches
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Neon Converter.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Neon Converter.lnk"
$sendTo = Join-Path $env:APPDATA "Microsoft\Windows\SendTo\Neon Converter.lnk"
$appsKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NeonConverter"

function Get-InstalledAppProcess {
    @(Get-Process NeonConverter -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith("$target\", [StringComparison]::OrdinalIgnoreCase) })
}

function Wait-ForAppToClose {
    # A running copy locks its files, so wait until the installed app is closed.
    if ((Get-InstalledAppProcess).Count) {
        Write-Host "Neon Converter is open - close it to continue..." -ForegroundColor Yellow
        while ((Get-InstalledAppProcess).Count) { Start-Sleep -Milliseconds 500 }
    }
}

function Remove-Folder($dir) {
    # Another program (antivirus, a finishing e-book engine) can hold a file for a moment, so retry
    # for a few seconds and report whether the folder is really gone.
    for ($i = 0; $i -lt 12 -and (Test-Path $dir); $i++) {
        if ($i) { Start-Sleep -Milliseconds 500 }
        try { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction Stop } catch { }
    }
    -not (Test-Path $dir)
}

function Get-FolderBytes($dir) {
    if (-not (Test-Path $dir)) { return 0 }
    $sum = (Get-ChildItem $dir -Recurse -File -Force | Measure-Object Length -Sum).Sum
    if ($sum) { [long]$sum } else { 0 }
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Wait-ForAppToClose
    Set-Location ([IO.Path]::GetTempPath())    # don't sit inside a folder that is being deleted
    foreach ($p in @($startMenu, $desktop, $sendTo)) { if (Test-Path $p) { Remove-Item $p -Force } }
    if (Test-Path $appsKey) { Remove-Item $appsKey -Recurse -Force }
    $left = @(foreach ($dir in @($target, $dataDir)) { if (-not (Remove-Folder $dir)) { $dir } })
    if ($left.Count) {
        Write-Host "Neon Converter is uninstalled, but Windows kept these folders in use:" -ForegroundColor Yellow
        $left | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
        Write-Host "Delete them yourself after restarting the PC." -ForegroundColor Yellow
        exit 0
    }
    Write-Host "Neon Converter removed: app, shortcuts, settings and log." -ForegroundColor Magenta
    exit 0
}

# ---------------------------------------------------------------- install
# Find the app: this script sits in _internal\ of an unzipped release, or at the repo root.
if (-not $Source) {
    foreach ($candidate in @($PSScriptRoot, (Split-Path $PSScriptRoot),
                             (Join-Path $PSScriptRoot "dist\NeonConverter"))) {
        if (Test-Path (Join-Path $candidate "NeonConverter.exe")) { $Source = $candidate; break }
    }
}
if (-not $Source -or -not (Test-Path (Join-Path $Source "NeonConverter.exe"))) {
    throw "NeonConverter.exe not found. Unzip the whole download first (or build with .\build.ps1, or pass -Source <folder>)."
}
$Source = (Resolve-Path $Source).Path.TrimEnd('\')

if ($Source -ne $target) {
    Wait-ForAppToClose
    $bytes = Get-FolderBytes $Source
    $drive = [IO.Path]::GetPathRoot($target)
    $needed = $bytes - (Get-FolderBytes $target) + 200MB
    $free = (New-Object IO.DriveInfo $drive).AvailableFreeSpace
    if ($free -lt $needed) {
        throw ("Not enough disk space on {0} - about {1:N1} GB more is needed." -f $drive, (($needed - $free) / 1GB))
    }
    Write-Host ("Copying Neon Converter ({0:N1} GB) to {1} ..." -f ($bytes / 1GB), $target) -ForegroundColor Cyan
    # Version 1.1.0 shipped some read-only files; make them replaceable before upgrading
    Get-ChildItem $target -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.IsReadOnly } | ForEach-Object { $_.IsReadOnly = $false }
    # /MIR also clears out files left over from an older version; /A-:R keeps every file writable
    robocopy $Source $target /MIR /A-:R /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying failed (robocopy error $LASTEXITCODE)." }
    $global:LASTEXITCODE = 0
}
if ($PSCommandPath -ne $installedScript) { Copy-Item $PSCommandPath $installedScript -Force }

# Files unzipped by Explorer carry Windows' "downloaded from the internet" mark. Clear it on the
# installed copy, as any installer does, so Windows doesn't warn again on every launch.
Get-ChildItem $target -Recurse -File -Force | Unblock-File

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

# List the app under Settings > Apps > Installed apps (per user, no admin needed)
$version = "$((Get-Item $exe).VersionInfo.ProductVersion)".Trim()
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$uninstallCmd = "`"$powershell`" -NoProfile -ExecutionPolicy Bypass -File `"$installedScript`" -Uninstall"
New-Item -Path $appsKey -Force | Out-Null
$strings = [ordered]@{
    DisplayName          = "Neon Converter"
    DisplayVersion       = $version
    DisplayIcon          = "$exe,0"
    InstallLocation      = $target
    UninstallString      = $uninstallCmd
    QuietUninstallString = $uninstallCmd
}
foreach ($name in $strings.Keys) {
    New-ItemProperty -Path $appsKey -Name $name -Value $strings[$name] -PropertyType String -Force | Out-Null
}
foreach ($name in @("NoModify", "NoRepair")) {
    New-ItemProperty -Path $appsKey -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
}
$sizeKB = [int]((Get-FolderBytes $target) / 1KB)
New-ItemProperty -Path $appsKey -Name EstimatedSize -Value $sizeKB -PropertyType DWord -Force | Out-Null

Write-Host ""
Write-Host "Neon Converter $version is installed." -ForegroundColor Magenta
Write-Host "  - Start menu and Desktop shortcuts, plus right-click a file > Send to > Neon Converter" -ForegroundColor Cyan
Write-Host "  - Uninstall any time from Settings > Apps > Installed apps" -ForegroundColor Cyan
if ($Source -ne $target) {
    Write-Host "  - The folder you installed from isn't needed any more; you can delete it and the zip." -ForegroundColor Cyan
}
if (-not $NoLaunch) { Start-Process $exe -WorkingDirectory $target }
exit 0
