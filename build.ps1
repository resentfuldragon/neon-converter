<#
.SYNOPSIS
  Builds Neon Converter into a standalone Windows app folder (dist\NeonConverter).

  Calibre's e-book engine is bundled inside the app (engines\calibre). It is taken from an
  installed Calibre, -CalibreDir, or downloaded from calibre-ebook.com automatically.

.EXAMPLE
  .\build.ps1            # build into .\dist
  .\build.ps1 -Zip       # also make dist\NeonConverter-windows.zip for a GitHub release
#>
param(
    [string]$DistPath = "dist",
    [string]$WorkPath = "build",
    [string]$CalibreDir = "",
    [switch]$NoCalibre,
    [switch]$Zip
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if (Test-Path ".venv\Scripts\python.exe") { (Resolve-Path ".venv\Scripts\python.exe").Path } else { "python" }
Write-Host "Using Python: $py"

if (-not (Test-Path "assets\icon.ico")) {
    & $py scripts\make_icon.py
    if ($LASTEXITCODE) { throw "Icon generation failed" }
}

# Stage the bundled command-line engines (FFmpeg, Pandoc) under plain names in bin\
New-Item -ItemType Directory -Force $WorkPath | Out-Null
$WorkPath = (Resolve-Path $WorkPath).Path
$stage = Join-Path $WorkPath "bin"
New-Item -ItemType Directory -Force $stage | Out-Null
$ffmpeg = (& $py -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()
$pandocDir = (& $py -c "import pypandoc, os; print(os.path.dirname(pypandoc.get_pandoc_path()))").Trim()
Copy-Item $ffmpeg (Join-Path $stage "ffmpeg.exe") -Force
Copy-Item (Join-Path $pandocDir "pandoc.exe") (Join-Path $stage "pandoc.exe") -Force

$assets = (Resolve-Path "assets").Path
$icon = Join-Path $assets "icon.ico"

# Name and version shown in the exe's Properties, Task Manager and Settings > Apps
$version = (& $py -c "import neonconvert; print(neonconvert.__version__)").Trim()
$nums = (@($version.Split('.')) + @('0', '0', '0', '0'))[0..3] -join ', '
$versionFile = Join-Path $WorkPath "version_info.txt"
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($nums), prodvers=($nums), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Neon Converter'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'NeonConverter'),
      StringStruct('OriginalFilename', 'NeonConverter.exe'),
      StringStruct('ProductName', 'Neon Converter'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Encoding Ascii $versionFile

# Remove the previous build first: older builds may hold read-only Calibre files PyInstaller can't delete
$previous = Join-Path $DistPath "NeonConverter"
if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }

& $py -m PyInstaller --noconfirm --clean --windowed `
    --name NeonConverter --icon $icon --version-file $versionFile `
    --distpath $DistPath --workpath $WorkPath --specpath $WorkPath `
    --add-data "$assets;assets" `
    --add-binary "$stage\ffmpeg.exe;bin" `
    --add-binary "$stage\pandoc.exe;bin" `
    --collect-data customtkinter `
    --collect-all tkinterdnd2 `
    --collect-submodules pymupdf4llm `
    --exclude-module pymupdf.layout `
    --exclude-module onnxruntime `
    --exclude-module imageio_ffmpeg `
    --exclude-module pypandoc `
    --exclude-module pytest `
    main.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$app = (Resolve-Path (Join-Path $DistPath "NeonConverter")).Path

# ---- bundle Calibre (GPL-3.0, redistributed unmodified) for MOBI/AZW3 and e-book conversions
if (-not $NoCalibre) {
    $src = & (Join-Path $PSScriptRoot "scripts\get_calibre.ps1") -CalibreDir $CalibreDir `
             -CacheDir (Join-Path $WorkPath "calibre-download")
    $src = @($src)[-1]
    $dest = Join-Path $app "_internal\engines\calibre"
    Write-Host "Bundling Calibre from $src ..." -ForegroundColor Cyan
    # /A-:R: the MSI unpacks Calibre read-only; ship normal files so unzipping, upgrading and deleting just work
    robocopy $src $dest /E /A-:R /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying Calibre failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
    $ver = (& (Join-Path $dest "ebook-convert.exe") --version | Select-Object -First 1) -replace '.*calibre\s+([\d.]+).*', '$1'
    @"
This folder contains calibre $ver by Kovid Goyal, redistributed unmodified under the
GNU General Public License v3 (see LICENSE in this folder).

Complete corresponding source code for this exact version:
  https://download.calibre-ebook.com/$ver/calibre-$ver.tar.xz
Project home: https://calibre-ebook.com  ·  https://github.com/kovidgoyal/calibre
"@ | Set-Content -Encoding UTF8 (Join-Path $dest "SOURCE.txt")
    Write-Host "Bundled calibre $ver" -ForegroundColor Cyan
}

# One-click installer for people who download the zip: "Install" next to NeonConverter.exe
Copy-Item (Join-Path $PSScriptRoot "install.ps1") (Join-Path $app "_internal\install.ps1") -Force
Copy-Item (Join-Path $PSScriptRoot "packaging\Install.cmd") (Join-Path $app "Install.cmd") -Force

Write-Host ""
Write-Host "Built: $app\NeonConverter.exe  (v$version)" -ForegroundColor Cyan

if ($Zip) {
    $zipPath = Join-Path (Resolve-Path $DistPath).Path "NeonConverter-windows.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
        tar.exe -a -c -f $zipPath -C (Split-Path $app) (Split-Path $app -Leaf)   # much faster than Compress-Archive
        if ($LASTEXITCODE) { throw "Zipping failed" }
    } else {
        Compress-Archive -Path $app -DestinationPath $zipPath -CompressionLevel Optimal
    }
    Write-Host ("Zipped: $zipPath  ({0:N0} MB)" -f ((Get-Item $zipPath).Length / 1MB)) -ForegroundColor Magenta
}
