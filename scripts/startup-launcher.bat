@echo off
setlocal
rem Resolve the project root from this script's own location.
set "PROJECT_ROOT=%~dp0.."
cd /d "%PROJECT_ROOT%"
call "%~dp0launch-dashboard.bat"
endlocal
