@echo off
REM Double-click this. It is the whole install.
REM
REM A .cmd rather than asking people to right-click setup.ps1: PowerShell
REM refuses to run downloaded scripts under the default execution policy, and
REM "Run with PowerShell" on a blocked file fails with a security error that
REM reads like the app is broken. Launching it this way sets the policy for
REM this one process only and changes nothing on the machine.

cd /d "%~dp0"

REM Files that came out of a zip are marked as downloaded, which makes
REM PowerShell refuse them even here. Clearing that is what Properties >
REM Unblock does, just without the hunting.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path '.' -Recurse -File -Include *.ps1,*.dll | Unblock-File -ErrorAction SilentlyContinue" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"

if errorlevel 1 (
  echo.
  echo   Setup stopped. Read the messages above - it will say what is missing.
  echo.
  pause
)
