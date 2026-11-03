@echo off
title Job Radar bot
:loop
cd /d "C:\path\to\job-radar"
py -3.13 bot.py
echo Bot stopped. Restarting in 15 seconds. Close this window to stop it.
timeout /t 15 /nobreak >nul
goto loop
