# Setting Up the Web Crawler Pipeline on a New Machine

This guide walks you through everything needed to get the pipeline running on your computer. Most of the setup is done through Claude Code inside VS Code, so you don't need to run complicated commands yourself.

**Estimated time:** 15–20 minutes

---

## Quick Setup (Recommended)

We provide an automated setup script that checks your system and installs most of what you need. This is the easiest way to get started.

### Before running the setup script

You need three things installed first. The setup script will check for these and tell you if any are missing:

1. **Python 3.11 or higher** — [download here](https://www.python.org/downloads/). **Important:** check the box "Add Python to PATH" during installation.
2. **Node.js (LTS version)** — [download here](https://nodejs.org/). Default settings are fine.
3. **Git** — [download here](https://git-scm.com/). Default settings are fine.

### Running the setup script

1. Download or clone the project repository (ask Long for access if needed)
2. Open the `web-crawler` folder on your computer
3. Double-click **`setup.bat`**
4. The script will walk you through everything:
   - It checks that Python, Node.js, Git, and rclone are installed
   - It installs all the Python libraries the pipeline needs
   - It downloads the browser engine (Chromium) used for scraping
   - It checks whether your credential files are in place
5. At the end, it gives you a clear summary of what's ready and what still needs attention

The script is safe to run multiple times — it won't break anything if you run it again.

### After the setup script finishes

The script will tell you if you still need to:

- **Add the `.env` file** — Ask Long for this file. It contains the API keys for the AI services. Place it in the root of the `web-crawler` folder.
- **Add the Google service account key** — Ask Long for this `.json` file. It allows the pipeline to access the Google Sheet and upload results to Drive.
- **Configure rclone** — This connects the pipeline to Google Drive for file uploads. Long will help you with this one-time setup (takes about 2 minutes).

---

## Detailed Manual Steps (If You Prefer)

If you'd rather set things up step by step (or if the setup script reports an issue), here's the full breakdown.

### Step 1: Install Python and Node.js

The pipeline uses two programming languages — Python and Node.js. Both need to be installed on your machine.

**Python:**

1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Download the latest version (3.11 or higher)
3. Run the installer
4. **Important:** Check the box that says "Add Python to PATH" before clicking Install

**Node.js:**

1. Go to [nodejs.org](https://nodejs.org/)
2. Download the LTS (Long Term Support) version
3. Run the installer and follow the prompts — the default settings are fine

### Step 2: Install rclone

rclone is the tool that allows the pipeline to upload files to Google Drive automatically.

1. Go to [rclone.org/downloads](https://rclone.org/downloads/)
2. Download the Windows version (AMD64 — Intel/AMD 64-bit)
3. Unzip the downloaded file
4. Move the `rclone.exe` file to a folder like `C:\rclone\`
5. Add that folder to your system PATH:
   - Open the Start menu and search for "Environment Variables"
   - Click "Edit the system environment variables"
   - Click the "Environment Variables" button
   - Under "User variables," find "Path" and click "Edit"
   - Click "New" and add `C:\rclone\`
   - Click OK on all windows

Once rclone is installed, Long will help you link it to the company Google Drive account. This is a one-time setup that takes about 2 minutes.

### Step 3: Clone the Repository

This downloads the pipeline code to your computer.

1. Open VS Code
2. Open the built-in terminal (press `` Ctrl + ` `` or go to Terminal > New Terminal)
3. Navigate to where you want to store the project, for example:
   ```
   cd C:\Users\YourName\Downloads
   ```
4. Run:
   ```
   git clone https://github.com/your-org/web-crawler.git
   ```
5. Open the downloaded folder in VS Code: File > Open Folder > select the `web-crawler` folder

If you don't have Git installed, VS Code will prompt you to install it, or you can download it from [git-scm.com](https://git-scm.com/).

### Step 4: Run the Setup Script or Let Claude Code Handle It

**Option A — Use the setup script (easiest):**

Double-click `setup.bat` in the `web-crawler` folder. It will install all dependencies and tell you what's ready.

**Option B — Use Claude Code:**

1. Make sure you have the `web-crawler` folder open in VS Code
2. Open the Claude Code panel (look for the Claude icon in the left sidebar, or press `Ctrl+Shift+P` and search for "Claude Code")
3. Ask Claude to install the project dependencies by typing something like:

   > "Install all the dependencies for this project — Python requirements and Playwright browser"

Claude Code will read the project configuration and run the necessary install commands for you.

### Step 5: Add the Credentials

This step connects the pipeline to the AI services and Google Drive.

**The .env file:**

1. Long will send you a file called `.env`
2. Place this file in the root of the `web-crawler` folder (right next to the existing files like `CLAUDE.md`)
3. This file contains the API keys — without it, the AI-powered parts of the pipeline won't work

**The Google service account key:**

1. Long will send you a `.json` file (the Google service account key)
2. Save it to a location on your computer (e.g., `C:\Users\YourName\Downloads\`)
3. Open the file `scrapers/dispatcher.py` in VS Code
4. Find the line near the top that says `SA_KEY_PATH = Path(...)` and update the file path to point to where you saved the `.json` file on your machine

If you're not sure how to do this last part, just ask Claude Code:

> "Update the Google service account key path in the dispatcher to point to C:\Users\YourName\Downloads\your-key-file.json"

### Step 6: Set Up the Daily Automation (Optional)

If you want the pipeline to run automatically every morning (just like it does on Long's machine), you need to set up a scheduled task.

1. Open the Start menu and search for **Task Scheduler**
2. Click "Create Basic Task"
3. Give it a name, e.g., "Web Crawler Dispatcher"
4. Set the trigger to **Daily** at **6:00 AM**
5. Set the action to **Start a program**
6. Set the program to your Python path (e.g., `C:\Users\YourName\AppData\Local\Programs\Python\Python311\python.exe`)
7. Set the arguments to: `scrapers/dispatcher.py`
8. Set "Start in" to the full path of the web-crawler folder (e.g., `C:\Users\YourName\Downloads\web-crawler`)
9. Click Finish

Alternatively, you can ask Claude Code to set this up for you:

> "Set up a Windows Task Scheduler job to run the dispatcher every day at 6 AM"

If you don't need the automation and prefer to run scrapes manually, you can skip this step entirely and just run requests on demand through Claude Code.

### Step 7: Test That Everything Works

To confirm the setup is complete:

1. Open the Claude Code panel in VS Code
2. Ask it to do a dry run of the dispatcher:

   > "Run the dispatcher in dry-run mode to check if everything is connected"

This will check if the system can download the Google Sheet and read the requests without actually running any scrapers. If it works, you'll see a list of current requests. If something is misconfigured, Claude will tell you what needs to be fixed.

---

## Troubleshooting

| Problem | What to do |
|---------|------------|
| "Python is not recognized" | Python wasn't added to PATH during installation. Reinstall and check the "Add to PATH" box. |
| "Node is not recognized" | Restart VS Code after installing Node.js. |
| "rclone: command not found" | Make sure rclone.exe is in your PATH (see Step 2). Restart VS Code. |
| Dispatcher can't download the sheet | The Google service account key path might be wrong, or rclone isn't configured. Ask Long for help. |
| Scraper fails with "API key" errors | The `.env` file is missing or in the wrong location. It should be in the root `web-crawler` folder. |

For any other issues, open Claude Code in VS Code and describe the error — it can usually diagnose and fix the problem for you.

---

## Quick Reference

| What | Where |
|------|-------|
| The code | GitHub repository (ask Long for access) |
| API keys and credentials | `.env` file (from Long) |
| Google service account key | `.json` file (from Long) |
| Scraping requests | [Google Sheet](https://docs.google.com/spreadsheets/d/1-fjYFJdx7zVJY6d8WXQpHtGFLJa9go9QmL-iYoy463s) |
| Scraped results | [Google Drive folder](https://drive.google.com/drive/folders/1Fmgyw28S4Xsu5ZRwJBofGXp3BMy1bt8E) |
| Daily automation | Windows Task Scheduler (6:00 AM) |
| Pipeline documentation | `docs/Pipeline Overview.docx` in the repository |
