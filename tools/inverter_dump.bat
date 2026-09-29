@echo off
rem Reads the inverter's web interface into one JSON file next to this file.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0inverter_dump.ps1" %*
pause
