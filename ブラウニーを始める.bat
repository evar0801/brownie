@echo off
chcp 65001 >nul
title Night runner
set PYTHONUTF8=1
python "%~dp0choose_projects.py"
if errorlevel 1 goto end
python "%~dp0runner.py"
:end
echo.
pause
