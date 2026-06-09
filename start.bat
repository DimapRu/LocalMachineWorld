@echo off
chcp 65001 >nul
title lmWorld Launcher

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

REM === Request admin privileges so file operations and Ollama setup work in any folder ===
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%ROOT%'"
    exit /b
)

echo [lmWorld] Running as administrator.
echo [lmWorld] Root directory: %ROOT%

REM Point Ollama at the workspace models folder so models pulled by the
REM "Hire Agent" flow live next to the project rather than in
REM %USERPROFILE%\.ollama\models. NOTE: this only takes effect if Ollama is
REM not already running. If ollama serve is already up with a different
REM OLLAMA_MODELS, models will land wherever that running process is
REM configured.
if not exist "%ROOT%\models" mkdir "%ROOT%\models"
set "OLLAMA_MODELS=%ROOT%\models"
echo [lmWorld] OLLAMA_MODELS=%OLLAMA_MODELS%

REM === Auto-download Ollama if not in project folder ===
set "OLLAMA_DIR=%ROOT%\ollama"
set "OLLAMA_EXE=%OLLAMA_DIR%\ollama.exe"
set "OLLAMA_ZIP=%OLLAMA_DIR%\ollama.zip"
set "OLLAMA_URL=https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"

REM Minimum reasonable zip size in bytes (~30 MB). Anything below is treated as
REM a corrupt / partial download and is wiped before re-fetching.
set "MIN_ZIP_SIZE=30000000"

:CHECK_OLLAMA
if exist "%OLLAMA_EXE%" goto OLLAMA_OK

REM Clean any half-extracted or partial state
if exist "%OLLAMA_DIR%" (
    rmdir /s /q "%OLLAMA_DIR%" 2>nul
)
mkdir "%OLLAMA_DIR%"

:RETRY_DOWNLOAD
echo [lmWorld] Ollama not found locally. Downloading portable zip from GitHub...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%OLLAMA_URL%' -OutFile '%OLLAMA_ZIP%' -UseBasicParsing } catch { Write-Error $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo [lmWorld] Download failed. Retrying in 3s...
    timeout /t 3 /nobreak >nul
    goto RETRY_DOWNLOAD
)

REM Integrity check: zip must be at least MIN_ZIP_SIZE bytes. If not, drop it and redownload.
for %%I in ("%OLLAMA_ZIP%") do set "ZIP_SIZE=%%~zI"
echo [lmWorld] Downloaded %ZIP_SIZE% bytes.
if %ZIP_SIZE% lss %MIN_ZIP_SIZE% (
    echo [lmWorld] Zip looks too small / corrupt. Deleting and re-downloading...
    del /f /q "%OLLAMA_ZIP%" 2>nul
    timeout /t 2 /nobreak >nul
    goto RETRY_DOWNLOAD
)

echo [lmWorld] Extracting Ollama into project root...
powershell -NoProfile -Command "Expand-Archive -Path '%OLLAMA_ZIP%' -DestinationPath '%OLLAMA_DIR%' -Force"
if errorlevel 1 (
    echo [lmWorld] Extraction failed. Corrupt archive, removing and retrying...
    del /f /q "%OLLAMA_ZIP%" 2>nul
    rmdir /s /q "%OLLAMA_DIR%" 2>nul
    mkdir "%OLLAMA_DIR%"
    goto RETRY_DOWNLOAD
)

REM If the exe is nested in a subfolder, move it up to %OLLAMA_DIR%.
if not exist "%OLLAMA_EXE%" (
    for /r "%OLLAMA_DIR%" %%F in (ollama.exe) do (
        if exist "%%F" (
            move /y "%%F" "%OLLAMA_EXE%" >nul
        )
    )
)

del /f /q "%OLLAMA_ZIP%" 2>nul

:OLLAMA_OK
if exist "%OLLAMA_EXE%" (
    echo [lmWorld] Ollama ready at %OLLAMA_EXE%
) else (
    echo [lmWorld] Warning: Ollama not downloaded yet. Hire Agent will prompt to download.
)

REM Start Ollama only if it isn't already listening on 11434.
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue)) { exit 1 } else { exit 0 }" >nul 2>&1
if errorlevel 1 (
    if exist "%OLLAMA_EXE%" (
        echo [lmWorld] Starting Ollama with OLLAMA_MODELS=%OLLAMA_MODELS% ...
        start "lmWorld Ollama" cmd /c "set OLLAMA_MODELS=%OLLAMA_MODELS%&& cd /d %OLLAMA_DIR% && ollama serve"
        timeout /t 2 /nobreak >nul
    ) else (
        echo [lmWorld] Warning: Ollama not downloaded yet. Hire Agent will prompt to download.
    )
) else (
    echo [lmWorld] Ollama already running on :11434
)

echo [lmWorld] Preparing backend on 127.0.0.1:8001...
cd /d "%ROOT%\backend"
if not exist "%ROOT%\backend" (
    echo ERROR: backend folder not found at %ROOT%\backend
    pause
    exit /b 1
)

REM Create an isolated backend venv on first launch so a fresh GitHub clone can run.
set "BACKEND_PY=%ROOT%\backend\.venv\Scripts\python.exe"
if not exist "%BACKEND_PY%" (
    echo [lmWorld] Backend virtual environment missing - creating .venv...
    python -m venv "%ROOT%\backend\.venv"
    if errorlevel 1 (
        echo [lmWorld] Failed to create backend virtual environment. Install Python 3.11+ and retry.
        pause
        exit /b 1
    )
)

echo [lmWorld] Installing/updating backend dependencies...
"%BACKEND_PY%" -m pip install --upgrade pip >nul
"%BACKEND_PY%" -m pip install -r "%ROOT%\backend\requirements.txt"
if errorlevel 1 (
    echo [lmWorld] Backend dependency installation failed.
    pause
    exit /b 1
)

REM Always replace an old backend so code changes are loaded and port 8001 is not stale.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $listeners = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8001 -State Listen; if ($listeners) { $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 }; foreach ($processId in $pids) { Write-Host ('[lmWorld] Stopping old backend process PID ' + $processId + ' on port 8001'); Stop-Process -Id $processId -Force } }; $deadline = (Get-Date).AddSeconds(15); do { Start-Sleep -Milliseconds 300; $stillListening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8001 -State Listen } while ($stillListening -and (Get-Date) -lt $deadline); if ($stillListening) { Write-Host '[lmWorld] ERROR: port 8001 is still busy after stopping old backend.'; exit 1 }"
if errorlevel 1 (
    echo [lmWorld] Could not free backend port 8001.
    pause
    exit /b 1
)

echo [lmWorld] Starting fresh backend...
start "lmWorld Backend" cmd /c ""%BACKEND_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log > backend.log 2>&1"

echo [lmWorld] Waiting for backend health check...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline = (Get-Date).AddSeconds(30); do { try { $res = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/' -TimeoutSec 2; if ($res.status -eq 'running') { Write-Host '[lmWorld] Backend is running.'; exit 0 } } catch {}; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $deadline); Write-Host '[lmWorld] ERROR: backend did not become ready in 30 seconds.'; exit 1"
if errorlevel 1 (
    echo [lmWorld] Backend failed to start. See %ROOT%\backend\backend.log
    pause
    exit /b 1
)

REM === Do NOT rebuild Tauri on every launch. It is slow and the built app can be reused. ===
REM To manually rebuild the desktop app after code changes, run: start.bat /build
if /i "%~1"=="/build" (
    call :TRY_BUILD_TAURI
    if errorlevel 1 (
        echo [lmWorld] Build failed. Continuing to launch the last available app/dev fallback.
    )
) else (
    echo [lmWorld] Skipping desktop rebuild. Use start.bat /build when you want to rebuild the bundled UI.
)
goto :DONE_BUILD_CHECK

:TRY_BUILD_TAURI
set "EXE_COUNT=0"
if exist "%ROOT%\frontend\src-tauri\target\release\lmworld.exe" set "EXE_COUNT=1"
if exist "%ROOT%\frontend\src-tauri\target\x86_64-pc-windows-msvc\release\lmworld.exe" set "EXE_COUNT=1"
if exist "%ROOT%\frontend\src-tauri\target\i686-pc-windows-msvc\release\lmworld.exe" set "EXE_COUNT=1"

if "%EXE_COUNT%"=="1" (
    echo [lmWorld] Manual rebuild requested. Rebuilding lmworld.exe...
) else (
    echo [lmWorld] Manual build requested. lmworld.exe not found, building desktop app...
)

if not exist "%ROOT%\frontend\node_modules" (
    echo [lmWorld] node_modules missing — running npm install...
    cd /d "%ROOT%\frontend"
    call npm install
)
cd /d "%ROOT%\frontend"
call npm run tauri -- build --no-bundle
if errorlevel 1 (
    echo [lmWorld] Tauri build FAILED. Falling back to dev server.
    exit /b 1
)
echo [lmWorld] Tauri build complete with the newest UI bundled.
exit /b 0

:DONE_BUILD_CHECK

REM Try to launch the desktop app first (bundled UI)
echo [lmWorld] Checking for desktop app (lmworld.exe)...
set "EXE_FOUND="

for %%p in (
    "%ROOT%\frontend\src-tauri\target\release\lmworld.exe"
    "%ROOT%\frontend\src-tauri\target\x86_64-pc-windows-msvc\release\lmworld.exe"
    "%ROOT%\frontend\src-tauri\target\i686-pc-windows-msvc\release\lmworld.exe"
    "%ROOT%\lmworld\lmworld.exe"
    "%ROOT%\lmworld.exe"
) do (
    if exist "%%~p" (
        set "EXE_FOUND=%%~p"
        goto :FOUND_EXE
    )
)

:FOUND_EXE
if defined EXE_FOUND (
    echo [lmWorld] Launching desktop app: %EXE_FOUND%
    start "" "%EXE_FOUND%"
    timeout /t 3 /nobreak >nul
    echo [lmWorld] Desktop app launched.
    echo [lmWorld] All services running. Close this window to stop.
    pause >nul
    goto :CLEANUP
)

echo [lmWorld] Desktop app not found. Starting dev server fallback at http://localhost:3000 ...
if not exist "%ROOT%\frontend\node_modules" (
    echo [lmWorld] Frontend dependencies missing - running npm install...
    cd /d "%ROOT%\frontend"
    call npm install
    if errorlevel 1 (
        echo [lmWorld] Frontend dependency installation failed.
        pause
        exit /b 1
    )
)
start "lmWorld Frontend" cmd /c "cd /d ""%ROOT%\frontend"" && npm run dev"
timeout /t 6 /nobreak >nul
start "" "http://localhost:3000"
echo [lmWorld] Dev server running at http://localhost:3000
echo [lmWorld] All services running. Close this window to stop.
pause >nul

:CLEANUP
taskkill /IM node.exe /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq lmWorld Frontend*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq lmWorld Backend*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq lmWorld Ollama*" /F >nul 2>&1
taskkill /IM lmworld.exe /F >nul 2>&1
echo [lmWorld] All services stopped.
