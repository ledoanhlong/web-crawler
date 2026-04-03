@echo off
title Web Crawler Pipeline - Setup
color 0A
echo.
echo  ===========================================================
echo    Web Crawler Pipeline - Automated Setup
echo  ===========================================================
echo.
echo  This script will check your system and install everything
echo  needed to run the pipeline. It will tell you if anything
echo  is missing that needs to be installed manually.
echo.
echo  ===========================================================
echo.
pause

set ERRORS=0
set WARNINGS=0

:: ---------------------------------------------------------------
::  Step 1: Check Python
:: ---------------------------------------------------------------
echo.
echo  [Step 1/7] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo    [X] Python is NOT installed.
    echo.
    echo        Please download and install Python 3.11 or higher from:
    echo        https://www.python.org/downloads/
    echo.
    echo        IMPORTANT: Check the box "Add Python to PATH" during installation.
    echo.
    set /a ERRORS+=1
) else (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo    [OK] Python %%v found
)

:: ---------------------------------------------------------------
::  Step 2: Check Node.js
:: ---------------------------------------------------------------
echo.
echo  [Step 2/7] Checking Node.js...
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo    [X] Node.js is NOT installed.
    echo.
    echo        Please download and install Node.js LTS from:
    echo        https://nodejs.org/
    echo.
    set /a ERRORS+=1
) else (
    for /f "tokens=1" %%v in ('node --version 2^>^&1') do echo    [OK] Node.js %%v found
)

:: ---------------------------------------------------------------
::  Step 3: Check Git
:: ---------------------------------------------------------------
echo.
echo  [Step 3/7] Checking Git...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo    [X] Git is NOT installed.
    echo.
    echo        Please download and install Git from:
    echo        https://git-scm.com/
    echo.
    set /a ERRORS+=1
) else (
    for /f "tokens=3" %%v in ('git --version 2^>^&1') do echo    [OK] Git %%v found
)

:: ---------------------------------------------------------------
::  Step 4: Check rclone
:: ---------------------------------------------------------------
echo.
echo  [Step 4/7] Checking rclone...
rclone --version >nul 2>&1
if %errorlevel% neq 0 (
    echo    [!] rclone is NOT installed.
    echo.
    echo        rclone is needed for Google Drive uploads.
    echo        Download it from: https://rclone.org/downloads/
    echo        Then add it to your system PATH.
    echo.
    echo        Ask Long to help you configure the Google Drive connection.
    echo.
    set /a WARNINGS+=1
) else (
    for /f "tokens=2" %%v in ('rclone --version 2^>^&1') do (
        echo    [OK] rclone %%v found
        goto :rclone_done
    )
    :rclone_done
)

:: ---------------------------------------------------------------
::  Stop here if core tools are missing
:: ---------------------------------------------------------------
if %ERRORS% gtr 0 (
    echo.
    echo  ===========================================================
    echo    [!] %ERRORS% required tool(s) missing.
    echo    Please install them and run this script again.
    echo  ===========================================================
    echo.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------
::  Step 5: Install Python dependencies
:: ---------------------------------------------------------------
echo.
echo  [Step 5/7] Installing Python dependencies...
echo           This may take a few minutes...
echo.
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo    [X] Failed to install Python dependencies.
    echo        Try running: pip install -r requirements.txt
    echo.
    set /a ERRORS+=1
) else (
    echo.
    echo    [OK] Python dependencies installed
)

:: ---------------------------------------------------------------
::  Step 6: Install Playwright browser (Chromium)
:: ---------------------------------------------------------------
echo.
echo  [Step 6/7] Installing Playwright browser (Chromium)...
echo           This downloads a browser the scrapers use behind the scenes.
echo           It may take a minute...
echo.
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo.
    echo    [X] Failed to install Playwright Chromium.
    echo        Try running: python -m playwright install chromium
    echo.
    set /a ERRORS+=1
) else (
    echo.
    echo    [OK] Playwright Chromium installed
)

:: ---------------------------------------------------------------
::  Step 7: Check for credentials
:: ---------------------------------------------------------------
echo.
echo  [Step 7/7] Checking for credentials...

if exist ".env" (
    echo    [OK] .env file found
) else (
    echo    [!] .env file is MISSING
    echo.
    echo        The .env file contains the API keys the pipeline needs.
    echo        Ask Long to send you this file, then place it in:
    echo        %cd%
    echo.
    set /a WARNINGS+=1
)

:: Check for Google service account key referenced in dispatcher
set SA_KEY_FOUND=0
if exist "C:\Users\%USERNAME%\Downloads\channelengine-ai-e7e32c629d8d.json" set SA_KEY_FOUND=1
if exist "%cd%\channelengine-ai-e7e32c629d8d.json" set SA_KEY_FOUND=1

if %SA_KEY_FOUND%==1 (
    echo    [OK] Google service account key found
) else (
    echo    [!] Google service account key not found
    echo.
    echo        This file allows the pipeline to access Google Sheets and Drive.
    echo        Ask Long to send you the .json key file.
    echo        Once you have it, you'll also need to update the file path
    echo        in scrapers/dispatcher.py (or ask Claude Code to do it for you).
    echo.
    set /a WARNINGS+=1
)

:: ---------------------------------------------------------------
::  Summary
:: ---------------------------------------------------------------
echo.
echo  ===========================================================
echo    SETUP COMPLETE
echo  ===========================================================
echo.

if %ERRORS% gtr 0 (
    echo    [X] %ERRORS% error(s) occurred during setup.
    echo        Review the messages above and fix the issues.
) else if %WARNINGS% gtr 0 (
    echo    [OK] All tools and dependencies are installed!
    echo.
    echo    [!] %WARNINGS% warning(s) - some credentials or optional
    echo        tools still need to be set up. See above for details.
) else (
    echo    [OK] Everything is set up and ready to go!
)

echo.
echo  -----------------------------------------------------------
echo    Next steps:
echo    1. Make sure your .env file is in place
echo    2. Make sure the Google service account key is configured
echo    3. Ask Long to help configure rclone (if not done yet)
echo    4. Open this folder in VS Code with Claude Code to get started
echo  -----------------------------------------------------------
echo.
pause
