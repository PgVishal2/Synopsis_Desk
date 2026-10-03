@echo off
REM Tests the API key in .env with a tiny AI call and explains any problem
cd /d "%~dp0"
python -m tender_synopsis check
pause
