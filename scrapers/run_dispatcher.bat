@echo off
REM Daily scraper dispatcher — scheduled via Windows Task Scheduler
REM Invokes Claude Code SDK to handle scraping requests from Google Sheet

cd /d "C:\Users\ledoa\Downloads\web-crawler"

REM Log start
echo [%date% %time%] Dispatcher starting >> output\logs\scheduler.log

REM Run the dispatcher
python scrapers\dispatcher.py >> output\logs\scheduler.log 2>&1

REM Log end
echo [%date% %time%] Dispatcher finished >> output\logs\scheduler.log
