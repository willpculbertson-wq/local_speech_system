@echo off
title Dictation System

:: Kill any existing instance
taskkill /F /FI "WINDOWTITLE eq Dictation System" /FI "IMAGENAME eq python.exe" >nul 2>&1

:: Activate conda dictation env and run
call C:\Users\willp\miniconda3\Scripts\activate.bat dictation
cd /d "%~dp0"
python src\main.py

pause
