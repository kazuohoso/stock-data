@echo off
REM FF会社概要 自動補填（Yahoo Finance Japan・非AI）週次ランナー
REM Windowsタスクスケジューラから起動される。gov-code-patrol が gov_ に突合・監視。
cd /d C:\projects\stock-data\daily_price_from_yahoofinance
"C:\Users\kazuo\AppData\Local\Python\pythoncore-3.14-64\python.exe" fetch_ff_overview_yahoojp.py >> ff_overview_yahoojp.log 2>&1
REM gov_ heartbeat (Inbox634 D-1 2026-11-02): success/error both
set RC=%ERRORLEVEL%
"C:\Users\kazuo\AppData\Local\Python\pythoncore-3.14-64\python.exe" C:\projects\_tools\gov_hb.py ff_overview_yahoojp_fill %RC% ff_overview_yahoojp.log >> ff_overview_yahoojp.log 2>&1
exit /b %RC%
