@echo off
setlocal
cd /d "%~dp0"

if not exist "web\dist\index.html" (
  if not exist "web\package.json" (
    echo [ERROR] Khong tim thay web\dist hoac web\package.json.
    pause
    exit /b 1
  )
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Can cai Node.js de build giao dien web.
    pause
    exit /b 1
  )
  pushd web
  if not exist node_modules call npm install
  if errorlevel 1 exit /b 1
  call npm run build
  if errorlevel 1 exit /b 1
  popd
)

python web_server.py
if errorlevel 1 pause
