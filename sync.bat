@echo off
REM ============================================================
REM  sync.bat  -  one-click safe commit + pull + push
REM  Usage:  double-click, or run:  sync.bat "your commit message"
REM ============================================================
cd /d "C:\Users\Owner\Personal Assistant"

REM 1. Clear any stale lock files left by an interrupted git run
del /q ".git\index.lock"              2>nul
del /q ".git\HEAD.lock"               2>nul
del /q ".git\objects\maintenance.lock" 2>nul

REM 2. Commit whatever you've changed (skips cleanly if nothing changed)
set "MSG=%~1"
if "%MSG%"=="" set "MSG=Update site"
git add -A
git commit -m "%MSG%"

REM 3. Pull the Action's commits (rebases your work on top), then push
git pull --rebase origin main
git push origin main

echo.
echo ============================================================
echo  Done. If it says "up to date" or "everything up-to-date",
echo  you're already in sync.
echo ============================================================
pause
