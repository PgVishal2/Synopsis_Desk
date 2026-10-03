@echo off
REM One-time setup: installs the Python packages and creates .env
cd /d "%~dp0"
python -m pip install -r requirements.txt || (echo Python not found - install Python 3.10+ from python.org and tick "Add to PATH" & pause & exit /b)
if not exist .env copy .env.example .env
echo.
echo Setup done. Now open the .env file in Notepad and paste your API key.
notepad .env
pause
