@echo off
if not exist "%~dp0state" mkdir "%~dp0state"
type nul > "%~dp0state\STOP"
echo STOP requested. The runner stops after the current session finishes.
echo To stop immediately, close the Night runner window.
timeout /t 5
