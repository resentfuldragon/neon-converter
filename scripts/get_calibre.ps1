<#
.SYNOPSIS
  Prints the path of a Calibre program folder to bundle into Neon Converter.

  Uses (in order) -CalibreDir, $env:CALIBRE_DIR, an installed Calibre in Program Files,
  or downloads the official Calibre MSI and unpacks it (an "administrative install":
  files only, nothing is installed on the machine).
#>
param(
    [string]$CalibreDir = "",
    [string]$CacheDir = (Join-Path $PSScriptRoot "..\build\calibre-download"),
    [switch]$SkipInstalled   # ignore an installed Calibre and use the official MSI instead
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster on PowerShell 5

function Test-Calibre($dir) { $dir -and (Test-Path (Join-Path $dir "ebook-convert.exe")) }

if (-not $SkipInstalled) {
    foreach ($candidate in @($CalibreDir, $env:CALIBRE_DIR,
                             (Join-Path $env:ProgramFiles "Calibre2"),
                             (Join-Path ${env:ProgramFiles(x86)} "Calibre2"))) {
        if (Test-Calibre $candidate) { return (Resolve-Path $candidate).Path }
    }
}

New-Item -ItemType Directory -Force $CacheDir | Out-Null
$CacheDir = (Resolve-Path $CacheDir).Path
$msi = Join-Path $CacheDir "calibre-64bit.msi"
# Calibre has deeply nested files; unpack to a short path or Windows Installer fails (error 1603)
$extract = Join-Path ([IO.Path]::GetTempPath()) "nc-calibre"

if (-not (Test-Path $msi)) {
    Write-Host "Downloading Calibre (official 64-bit MSI)..." -ForegroundColor Cyan
    Invoke-WebRequest "https://calibre-ebook.com/dist/win64" -OutFile $msi -UseBasicParsing
}
if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
Write-Host "Unpacking Calibre (no install)..." -ForegroundColor Cyan
$p = Start-Process msiexec.exe -ArgumentList "/a `"$msi`" /qn TARGETDIR=`"$extract`"" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "msiexec /a failed with exit code $($p.ExitCode)" }

$exe = Get-ChildItem $extract -Recurse -Filter "ebook-convert.exe" | Select-Object -First 1
if (-not $exe) { throw "Unpacked the Calibre MSI but found no ebook-convert.exe" }
return $exe.DirectoryName
