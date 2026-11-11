@echo off
setlocal

:: =========================================================================
:: Ghost-Data Suite — Windows Launcher
:: Follows windows-local-web-app-packaging skill patterns
:: =========================================================================

:: 1. Environment cleanup (prevent leaks from parent shells)
set PYTHONPATH=
set PYTHONHOME=
set VIRTUAL_ENV=

:: 2. Paths
set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "APP_NAME=Ghost-Data Suite"
set "PORT=5000"

:: 3. Use the Hermes venv Python (known to have all required packages)
set "PYTHON=C:\Users\user\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

:: Verify it can import required packages
"%PYTHON%" -c "import flask, flask_socketio, playwright, cryptography, flask_limiter, prometheus_client, yaml, alembic" >nul 2>&1
if errorlevel 1 (
    echo [%APP_NAME%] ERROR: Hermes venv Python is missing required packages.
    echo [%APP_NAME%] Install: pip install flask flask-socketio playwright cryptography flask-limiter prometheus-client pyyaml alembic python-dateutil
    pause
    exit /b 1
)

echo [%APP_NAME%] Using Python: %PYTHON%
"%PYTHON%" --version

:: 4. Generate required secrets if missing (server fails fast without them)
echo [%APP_NAME%] Checking required secrets...
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[System.Guid]::NewGuid().ToString('N')"`) do set "GHOST_AUTH_TOKEN=%%t"
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "[System.Guid]::NewGuid().ToString('N')"`) do set "SECRET_KEY=%%s"
echo [%APP_NAME%] Generated GHOST_AUTH_TOKEN
echo [%APP_NAME%] Generated SECRET_KEY

:: 4b. Write auth config for frontend (no prompts, no tokens in URLs)
echo [%APP_NAME%] Writing frontend auth config...
echo window.GHOST_AUTH_TOKEN = "%GHOST_AUTH_TOKEN%"; > "%ROOT%\static\auth-config.js"
echo window.GHOST_SESSION_TOKEN = "%GHOST_AUTH_TOKEN%"; >> "%ROOT%\static\auth-config.js"

:: 5. Ensure data directories exist
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
if not exist "%ROOT%\keys" mkdir "%ROOT%\keys"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\media" mkdir "%ROOT%\media"

:: 6. Avoid duplicate backend starts
netstat -ano | findstr :%PORT% | findstr LISTENING >nul
if not errorlevel 1 (
    echo [%APP_NAME%] Backend already listening on port %PORT%.
) else (
    echo [%APP_NAME%] Starting backend on port %PORT%...
    set "GHOST_PORT=%PORT%"
    start "GhostBackend" /B "%PYTHON%" "%ROOT%\src\ghost_server.py"
)

:: 7. Wait for readiness (PowerShell with -UseBasicParsing to avoid script-execution prompt)
echo [%APP_NAME%] Waiting for backend to be ready...
set /a ATTEMPTS=0
:wait_backend
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [%APP_NAME%] Backend is ready.
) else (
    set /a ATTEMPTS+=1
    if %ATTEMPTS% leq 30 (
        powershell -NoProfile -Command "Start-Sleep -Seconds 1"
        goto wait_backend
    )
    echo [%APP_NAME%] WARNING: Backend did not become ready in time. Opening browser anyway.
)

:: 8. Open the dashboard in the default browser
echo [%APP_NAME%] Opening dashboard...
start "" "http://127.0.0.1:%PORT%/"

echo [%APP_NAME%] Launched. Backend is running in background.
endlocal
