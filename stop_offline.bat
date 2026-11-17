@echo off
title Stop Tamil Document Studio
echo Stopping Tamil Document Studio processes...

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a 2>nul
)

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :3000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a 2>nul
)

echo All services on ports 8000 and 3000 have been stopped.
timeout /t 3 /nobreak > nul
