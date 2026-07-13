@echo off
rem Wavelog Offline-Kit - Windows-Starter.
rem Umgeht die PowerShell-Ausfuehrungsrichtlinie fuer heruntergeladene Scripts
rem (nur fuer diesen einen Aufruf, keine Systemeinstellung wird geaendert).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
