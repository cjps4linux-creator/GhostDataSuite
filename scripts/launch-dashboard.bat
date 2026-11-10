@echo off
setlocal
pushd "%~dp0\.."

echo [ghost-suite] launching...

where docker >nul 2>nul
if %errorlevel%==0 (
    echo [ghost-suite] docker detected, starting stack...
    start /min "" docker compose up -d --build
    echo [ghost-suite] waiting for services...
    powershell -Command "Start-Sleep -Seconds 6"
    echo [ghost-suite] opening dashboard...
    start http://localhost:5000/
) else (
    echo [ghost-suite] docker not found, starting local server...
    start /min "" python -m src.ghost_server
    powershell -Command "Start-Sleep -Seconds 2"
    echo [ghost-suite] opening dashboard...
    start http://localhost:5000/
)

popd
endlocal
