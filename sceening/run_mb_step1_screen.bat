@echo off
REM mb STEP1 weekly screening (multibagger experiment lab) - run by Windows Task Scheduler
cd /d C:\projects\stock-data\sceening
python mb_step1_screen.py >> mb_step1_screen.log 2>&1
REM gov_ heartbeat (Inbox634 D-1 2026-11-02): success/error both
set RC=%ERRORLEVEL%
python C:\projects\_tools\gov_hb.py mb_step1_screen %RC% mb_step1_screen.log >> mb_step1_screen.log 2>&1
exit /b %RC%
