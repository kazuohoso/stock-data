@echo off
REM mb daily tracker (multibagger experiment lab) - run by Windows Task Scheduler
cd /d C:\projects\stock-data\sceening
python mb_daily_tracker.py >> mb_tracker.log 2>&1
