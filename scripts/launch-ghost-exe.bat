@echo off
setlocal EnableDelayedExpansion

:: =========================================================================
:: Ghost-Data Suite — EXE Launcher Wrapper
:: =========================================================================

set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "APP_NAME=Ghost-Data Suite"
set "PORT=5000"
set "EXE=%ROOT%dist\ghost-data-suite.exe"

echo [%APP_NAME%] Ghost-Data Suite Launcher
echo [%APP_NAME%] Working directory: %CD%

:: 1. Verify the .exe exists
if not exist "%EXE%" (
    echo [%APP_NAME%] ERROR: %EXE% not found.
    echo [%APP_NAME%] Build it first with: pyinstaller ghost-data-suite.spec --clean --noconfirm
    pause
    exit /b 1
)

:: 2. Kill any existing process on port 5000 (prevents bind crashes)
echo [%APP_NAME%] Checking port %PORT%...
netstat -ano | findstr ":%PORT%" | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo [%APP_NAME%] Port %PORT% is in use. Freeing it...
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
        taskkill /PID %%p /F >nul 2>&1
        echo [%APP_NAME%] Terminated process %%p
    )
    timeout /t 2 >nul
)

:: 3. Generate required secrets if missing
for /f "usebackq delims=" %%t in ('powershell -NoProfile -Command "[System.Guid]::NewGuid().ToString('N')"') do set "GHOST_AUTH_TOKEN=%%t"
for /f "usebackq delims=" %%s in ('powershell -NoProfile -Command "[System.Guid]::NewGuid().ToString('N')"') do set "SECRET_KEY=%%s"
echo [%APP_NAME%] Generated GHOST_AUTH_TOKEN
echo [%APP_NAME%] Generated SECRET_KEY

:: 3b. Set Playwright browsers path (required for scraping in .exe)
set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
if not exist "%PLAYWRIGHT_BROWSERS_PATH%" (
    echo [%APP_NAME%] WARNING: Playwright browsers not found at %PLAYWRIGHT_BROWSERS_PATH%
    echo [%APP_NAME%] Scraping will fail. Run: playwright install chromium
) else (
    echo [%APP_NAME%] Playwright browsers found at %PLAYWRIGHT_BROWSERS_PATH%
)

:: 4. Write frontend auth config (no prompts, no tokens in URLs)
echo [%APP_NAME%] Writing frontend auth config...
echo window.GHOST_AUTH_TOKEN = "%GHOST_AUTH_TOKEN%"; > "%ROOT%static\auth-config.js"
echo window.GHOST_SESSION_TOKEN = "%GHOST_AUTH_TOKEN%"; >> "%ROOT%static\auth-config.js"

:: 5. Ensure data directories exist
if not exist "%ROOT%data" mkdir "%ROOT%data"
if not exist "%ROOT%keys" mkdir "%ROOT%keys"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
if not exist "%ROOT%media" mkdir "%ROOT%media"

:: 6. Launch the .exe
echo [%APP_NAME%] Starting %EXE%...
set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
start "GhostSuite" "%EXE%"

:: 7. Wait briefly for the backend to be ready
echo [%APP_NAME%] Waiting for backend to be ready...
set /a ATTEMPTS=0
:wait_backend
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [%APP_NAME%] Backend is ready.
) else (
    set /a ATTEMPTS+=1
    if !ATTEMPTS! leq 30 (
        timeout /t 1 >nul
        goto wait_backend
    )
    echo [%APP_NAME%] WARNING: Backend did not become ready in time.
)

:: 8. Open the dashboard
echo [%APP_NAME%] Opening dashboard...
start "" "http://127.0.0.1:%PORT%/"

echo [%APP_NAME%] Launched. Backend is running. Press any key to close this window.
pause >nul
endlocal
