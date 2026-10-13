@echo off
setlocal DisableDelayedExpansion
rem Let Windows PowerShell use its own modules, including when called from pwsh.
set PSModulePath=
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
exit /b %ERRORLEVEL%
