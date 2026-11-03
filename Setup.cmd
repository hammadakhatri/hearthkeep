@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Hearthkeep setup
cd /d "%~dp0" || goto end
rem When UV_VERSION changes, recompute UV_INSTALLER_SHA256 from the downloaded file: Invoke-WebRequest -OutFile, then Get-FileHash.
set "UV_VERSION=0.13.0"
set "UV_INSTALLER_SHA256=6D7EF89BA04838A03D1EBF7F84BE4BA2B98B848A0A9AB8C2855658F13106C2C2"
set "UV_INSTALL_DIR=%USERPROFILE%\.local\bin"
set "UV_EXE=%UV_INSTALL_DIR%\uv.exe"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
rem A PowerShell 7 window passes down its module path, which hides Get-FileHash from Windows PowerShell.
set "PSModulePath="
echo.
echo Step 1 of 3: Checking for uv, the tool that runs Hearthkeep...
if exist "%UV_EXE%" goto run
for /f "delims=" %%U in ('where uv 2^>nul') do (set "UV_EXE=%%U" & goto run)
echo Installing uv for your Windows account. No admin rights are needed.
"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $f=Join-Path $env:TEMP ('uv-install-'+[guid]::NewGuid()+'.ps1'); Invoke-WebRequest -UseBasicParsing -Uri ('https://astral.sh/uv/'+$env:UV_VERSION+'/install.ps1') -OutFile $f; if ((Get-FileHash -Algorithm SHA256 $f).Hash -ne $env:UV_INSTALLER_SHA256) { Remove-Item $f; exit 3 }; $env:UV_NO_MODIFY_PATH='1'; & $f; $c=$LASTEXITCODE; Remove-Item $f; exit $c"
if errorlevel 3 if not errorlevel 4 (echo The uv installer didn't match its expected checksum, so it was not run. Try again later. & goto end)
if not exist "%UV_EXE%" (echo uv couldn't be installed. Check your internet connection, then double-click Setup.cmd again. & goto end)
:run
"%UV_EXE%" run --project server hearthkeep setup
:end
echo.
echo Press any key to close this window.
pause >nul
