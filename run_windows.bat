@echo off
REM Starts the Tender Synopsis Generator in your browser
cd /d "%~dp0"
python -m streamlit run app.py
pause
