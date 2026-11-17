@echo off
title Tamil Document Studio (Offline)
echo ========================================================
echo   Starting Tamil English Document Studio (Offline Mode)
echo ========================================================
echo.

cd /d "%~dp0"

echo [1/3] Starting Backend API (Port 8000)...
start "Tamil OCR - Backend (Port 8000)" cmd /k "cd /d ""%~dp0backend"" && set PYTHONPATH=.&& .venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000"

echo [2/3] Starting Frontend Web App (Port 3000)...
start "Tamil OCR - Frontend (Port 3000)" cmd /k "cd /d ""%~dp0frontend"" && npm run dev"

echo [3/3] Waiting for servers to initialize...
timeout /t 5 /nobreak > nul

echo Opening browser at http://localhost:3000 ...
start http://localhost:3000

echo.
echo ========================================================
echo   Tamil Document Studio is running!
echo   Frontend : http://localhost:3000
echo   Backend  : http://127.0.0.1:8000
echo.
echo   To stop the application, run stop_offline.bat
echo ========================================================
