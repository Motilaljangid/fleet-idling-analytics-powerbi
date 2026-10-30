@echo off
REM Runs the ETL from the folder this file sits in. Schedule this file in Task Scheduler.
cd /d "%~dp0"
echo ===== %date% %time% ===== >> etl_run_log.txt
python idling_etl.py >> etl_run_log.txt 2>&1
