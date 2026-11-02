@echo off
REM mb daily tracker (multibagger experiment lab) - run by Windows Task Scheduler
cd /d C:\projects\stock-data\sceening
python mb_daily_tracker.py >> mb_tracker.log 2>&1
REM gov_ heartbeat (Inbox634 D-1 2026-11-02): success/error both
set RC=%ERRORLEVEL%
python C:\projects\_tools\gov_hb.py mb_daily_tracker %RC% mb_tracker.log >> mb_tracker.log 2>&1
exit /b %RC%
