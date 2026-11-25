@echo off
setlocal
title Neon Converter setup
rem One-click install for this Windows user, no admin rights needed: copies the app to
rem your Programs folder, adds Start menu, Desktop and right-click "Send to" shortcuts,
rem and lists it under Settings - Apps so it can be uninstalled there.
rem (Running NeonConverter.exe straight from the unzipped folder works too.)

if not exist "%~dp0_internal\install.ps1" (
    echo Unzip the whole download first: right-click the zip and choose Extract All,
    echo then double-click Install inside the unzipped NeonConverter folder.
    echo.
    pause
    exit /b 1
)

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\install.ps1" %*
if errorlevel 1 (
    echo.
    echo Setup did not finish - see the message above.
    pause
    exit /b 1
)
timeout /t 5 >nul 2>&1
exit /b 0
