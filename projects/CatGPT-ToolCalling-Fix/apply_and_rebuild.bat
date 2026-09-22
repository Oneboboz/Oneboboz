@echo off
setlocal
python patch_toolcalling.py
if errorlevel 1 (
  echo [FAILED] Patch failed.
  pause
  exit /b 1
)

echo.
echo [OK] Rebuilding CatGPT...
docker compose build --no-cache
if errorlevel 1 (
  echo [FAILED] Docker build failed.
  pause
  exit /b 1
)

echo.
echo [OK] Restarting CatGPT...
docker compose up -d
if errorlevel 1 (
  echo [FAILED] Docker compose up failed.
  pause
  exit /b 1
)

echo.
docker compose ps
pause
